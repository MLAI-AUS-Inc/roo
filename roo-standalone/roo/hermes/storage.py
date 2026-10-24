"""Read-only checks for the task volume; quota assignment belongs to lifecycle.

A writable bind mount on the host root filesystem has no per-container Docker
size limit. Do not claim output limits or memory limits bound its disk usage.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class TaskStorage:
    mountpoint: Path
    device: str
    total_bytes: int
    available_bytes: int
    project_quotas: bool


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def inspect_task_storage(root: Path, *, max_filesystem_bytes: int,
                         mountinfo: str | None = None, statvfs=None) -> TaskStorage:
    """Require a separate bounded XFS filesystem with enforced project quotas.

    Does not format/mount anything, assign a project, or change quotas. The
    privileged lifecycle controller must set and verify a hard byte+inode quota
    on every attempt directory before starting its container.
    """
    root = Path(root)
    if (not root.is_absolute() or root.resolve() != root or root == Path("/")
            or not root.is_dir() or max_filesystem_bytes <= 0):
        raise ValueError("Invalid canonical task storage directory or capacity limit")
    if mountinfo is None:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    mounts = []
    for line in mountinfo.splitlines():
        before, marker, after = line.partition(" - ")
        fields, extra = before.split(), after.split()
        if not marker or len(fields) < 6 or len(extra) < 3:
            raise ValueError("Invalid mount inventory")
        mountpoint = Path(_unescape_mount(fields[4]))
        if root == mountpoint or mountpoint in root.parents:
            mounts.append((mountpoint, fields, extra))
    if not mounts:
        raise ValueError("Task storage is not present in the mount inventory")
    mountpoint, fields, extra = max(mounts, key=lambda item: len(item[0].parts))
    # A bind/subtree mount does not establish a dedicated filesystem boundary.
    # The device must also differ from the root device, not merely its path.
    root_devices = {line.split()[2] for line in mountinfo.splitlines()
                    if len(line.split()) >= 5 and line.split()[4] == "/"}
    options = set(fields[5].split(",")) | set(extra[2].split(","))
    if (mountpoint == Path("/") or fields[2] in root_devices
            or fields[3] != "/" or extra[0] != "xfs"
            or not options.intersection({"prjquota", "pquota"})
            or "pqnoenforce" in options or "ro" in options):
        raise ValueError("Task storage requires a separate writable XFS filesystem with enforced project quotas")
    facts = (statvfs or os.statvfs)(root)
    total = facts.f_blocks * facts.f_frsize
    available = facts.f_bavail * facts.f_frsize
    if not 0 < total <= max_filesystem_bytes:
        raise ValueError("Task filesystem exceeds the reviewed capacity ceiling")
    return TaskStorage(mountpoint, fields[2], total, available, True)
