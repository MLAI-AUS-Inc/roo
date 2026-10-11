#!/usr/bin/python3 -I
"""Root-owned fixed-policy XFS quota helper. Never run from a writable checkout.

Installed alongside storage_check.py in /usr/local/lib/roo-hermes-storage and
invoked through the fixed launcher. There is no environment/config-path override.
"""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

sys.path.insert(0, "/usr/local/lib/roo-hermes-storage")
from storage_check import inspect_task_storage

CONFIG = Path("/etc/roo-hermes/quota.json")
COUNTER_ROOT = Path("/var/lib/roo-hermes-quota")
TASK_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{32}")
XFS_QUOTA = "/usr/sbin/xfs_quota"
XFS_IO = "/usr/sbin/xfs_io"


def protected(path: Path, *, directory=False):
    info = path.lstat()
    if (info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink()
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise ValueError("Unsafe privileged helper file")
    return info


def trusted_parents(path):
    for parent in (path, *path.parents):
        protected(parent, directory=True)


def load_config():
    trusted_parents(CONFIG.parent)
    protected(CONFIG)
    if CONFIG.stat().st_size > 8192:
        raise ValueError("Invalid quota configuration")
    data = json.loads(CONFIG.read_text())
    if set(data) != {"task_root", "controller_uid", "controller_gid", "quota_bytes", "quota_inodes", "max_filesystem_bytes", "first_project_id"}:
        raise ValueError("Invalid quota configuration")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", data["task_root"]) or "//" in data["task_root"]:
        raise ValueError("Invalid fixed task root")
    for name in set(data) - {"task_root"}:
        if type(data[name]) is not int:
            raise ValueError("Invalid integer quota configuration")
    if (data["controller_uid"] != 10000 or data["controller_gid"] != 10000
            or not 67_108_864 <= data["quota_bytes"] <= 8_589_934_592
            or data["quota_bytes"] % 1024
            or not 1024 <= data["quota_inodes"] <= 1_000_000
            or not data["quota_bytes"] <= data["max_filesystem_bytes"] <= 107_374_182_400
            or not 100_000 <= data["first_project_id"] < 2_000_000_000):
        raise ValueError("Quota configuration is outside policy bounds")
    root = Path(data["task_root"])
    trusted_parents(root.parent)
    if root.resolve() != root:
        raise ValueError("Task root must be canonical")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != data["controller_uid"] or info.st_gid != data["controller_gid"] or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Task root must be private and controller owned")
    return data


def run_tool(argv, *, pass_fds=()):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True, timeout=20,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
                            pass_fds=pass_fds)
    if result.stderr or len(result.stdout) > 65536:
        raise ValueError("Unexpected quota tool diagnostics")
    return result.stdout.decode("ascii", errors="strict")


def quota_command(mountpoint, command, *, pass_fds=()):
    # -D/-P prevent a system /etc/projects mapping widening the selected tree.
    return run_tool([XFS_QUOTA, "-x", "-D", "/dev/null", "-P", "/dev/null", "-c", command, str(mountpoint)], pass_fds=pass_fds)


def check_enforcement(mountpoint):
    text = quota_command(mountpoint, "state -p")
    if not re.search(r"Accounting:\s+ON\b", text) or not re.search(r"Enforcement:\s+ON\b", text):
        raise ValueError("Project quota enforcement must be active")


def reserve_project(data):
    """Caller holds the stable lock file; fsynced replacement burns each ID."""
    counter = COUNTER_ROOT / "next-project-id"
    if counter.exists() or counter.is_symlink():
        protected(counter)
        value = counter.read_text()
        if not re.fullmatch(r"[0-9]{1,10}\n?", value):
            raise ValueError("Invalid project counter")
        project = int(value)
    else:
        project = data["first_project_id"]
    if not data["first_project_id"] <= project < 2_000_000_000:
        raise ValueError("Project counter exhausted")
    temporary = COUNTER_ROOT / "next-project-id.new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, f"{project + 1}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, counter)
    directory = os.open(COUNTER_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return project


def verify_project(fd, project):
    text = run_tool([XFS_IO, "-r", "-c", "stat", f"/proc/self/fd/{fd}"], pass_fds=(fd,))
    actual = re.search(r"fsxattr.projid\s*=\s*(\d+)\b", text)
    flags = re.search(r"fsxattr.xflags\s*=\s*(0x[0-9a-fA-F]+)\b", text)
    if not actual or int(actual[1]) != project or not flags or not int(flags[1], 16) & 0x200:
        raise ValueError("Project ID or inherited project flag was not applied")


def verify_limit(text, project, expected):
    # xfs_quota report -n -N uses numeric/#numeric IDs, default block units 1KiB.
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) < 4 or rows[0][0].lstrip("#") != str(project):
        raise ValueError("Quota report did not identify the assigned project")
    if not rows[0][2].isdigit() or not rows[0][3].isdigit() or int(rows[0][2]) != expected or int(rows[0][3]) != expected:
        raise ValueError("Hard and soft quota limits were not confirmed")


def prepare(data, attempt, storage):
    if not TASK_NAME.fullmatch(attempt):
        raise ValueError("Invalid attempt identifier")
    trusted_parents(COUNTER_ROOT)
    with ExitStack() as cleanup:
        lock = os.open(COUNTER_ROOT / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        cleanup.callback(os.close, lock)
        protected(COUNTER_ROOT / "lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = os.open(data["task_root"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        cleanup.callback(os.close, root)
        # Freeze controller-side namespace changes during the privileged calls.
        # A process crash may leave these dirs root-owned: fail closed and repair
        # manually with every task stopped. Container mounts don't include root.
        os.fchown(root, 0, 0)
        cleanup.callback(os.fchown, root, data["controller_uid"], data["controller_gid"])
        target = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        cleanup.callback(os.close, target)
        descriptors = [target]
        for name in (None, "workspace", "state"):
            fd = target if name is None else os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target)
            if name:
                cleanup.callback(os.close, fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if (info.st_uid != data["controller_uid"] or info.st_gid != data["controller_gid"]
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_dev != os.fstat(root).st_dev):
                raise ValueError("Unsafe task directory")
            os.fchown(fd, 0, 0)
            cleanup.callback(os.fchown, fd, data["controller_uid"], data["controller_gid"])
        if set(os.listdir(target)) != {"workspace", "state"} or any(os.listdir(fd) for fd in descriptors[1:]):
            raise ValueError("Only a fresh empty task attempt can receive a quota")
        project = reserve_project(data)
        mountpoint = storage.mountpoint
        check_enforcement(mountpoint)
        quota_command(mountpoint, f"limit -p bsoft={data['quota_bytes']} bhard={data['quota_bytes']} isoft={data['quota_inodes']} ihard={data['quota_inodes']} {project}")
        # Namespace is now root-owned; the path cannot be swapped by UID10000.
        path = str(Path(data["task_root"]) / attempt)
        quota_command(mountpoint, f"project -s -p {path} {project}")
        for fd in descriptors:
            verify_project(fd, project)
        for unit, expected in (("b", data["quota_bytes"] // 1024), ("i", data["quota_inodes"])):
            report = quota_command(mountpoint, f"report -p -{unit} -n -N -L {project} -U {project}")
            verify_limit(report, project, expected)
        return project


def main():
    def deadline(*_):
        raise TimeoutError("Quota helper deadline")

    # Finish cleanup before the outer controller's 30-second kill deadline.
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(20)
    try:
        if os.geteuid() != 0 or sys.argv[1:] not in (["check"],) and (len(sys.argv) != 3 or sys.argv[1] != "prepare"):
            raise ValueError("Usage: roo-hermes-quota check | prepare UUID-ATTEMPT")
        data = load_config()
        storage = inspect_task_storage(Path(data["task_root"]), max_filesystem_bytes=data["max_filesystem_bytes"])
        check_enforcement(storage.mountpoint)
        result = {"ok": True, "task_root": data["task_root"], "mountpoint": str(storage.mountpoint),
                  "quota_bytes": data["quota_bytes"], "quota_inodes": data["quota_inodes"]}
        if sys.argv[1] == "prepare":
            result.update(attempt=sys.argv[2], project_id=prepare(data, sys.argv[2], storage))
        print(json.dumps(result))
    except Exception:
        print("Hermes quota preflight/preparation failed; inspect host policy and XFS state", file=sys.stderr)
        raise SystemExit(1)
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    main()
