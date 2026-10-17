import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from roo.hermes import storage

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def quota(monkeypatch):
    monkeypatch.setitem(sys.modules, "storage_check", storage)
    spec = importlib.util.spec_from_file_location("roo_test_quota", ROOT / "hermes/storage/quota.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_attempt_name_cannot_select_a_path_or_command(quota):
    good = "01111111-2222-4333-8444-555555555555-" + "a" * 32
    assert quota.TASK_NAME.fullmatch(good)
    for value in ("../" + good, good + "/state", good + "\n", good + " ; id", good.upper()):
        assert not quota.TASK_NAME.fullmatch(value)


def test_id_reservation_persists_without_reusing_id(quota, tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "COUNTER_ROOT", tmp_path)
    monkeypatch.setattr(quota, "protected", lambda *args, **kwargs: None)
    assert quota.reserve_project({"first_project_id": 100000}) == 100000
    assert quota.reserve_project({"first_project_id": 100000}) == 100001
    assert (tmp_path / "next-project-id").read_text() == "100002\n"
    (tmp_path / "next-project-id").write_text("bad counter")
    with pytest.raises(ValueError):
        quota.reserve_project({"first_project_id": 100000})


def test_readback_rejects_disabled_enforcement_and_wrong_limits(quota, monkeypatch):
    monkeypatch.setattr(quota, "quota_command", lambda *_: "Accounting: ON\nEnforcement: OFF\n")
    with pytest.raises(ValueError):
        quota.check_enforcement(Path("/tasks"))
    quota.verify_limit("#100001 0 2048 2048 00 [--------]\n", 100001, 2048)
    for output in ("", "#100002 0 2048 2048", "#100001 0 2048 0", "#100001 0 1 1\n#100001 0 1 1"):
        with pytest.raises(ValueError):
            quota.verify_limit(output, 100001, 2048)


def test_project_readback_requires_id_and_inherit(quota, monkeypatch):
    monkeypatch.setattr(quota, "run_tool", lambda *a, **kw: "fsxattr.projid = 100001\nfsxattr.xflags = 0x200 [proj-inherit]\n")
    quota.verify_project(42, 100001)
    with pytest.raises(ValueError):
        quota.verify_project(42, 100002)
    monkeypatch.setattr(quota, "run_tool", lambda *a, **kw: "fsxattr.projid = 100001\nfsxattr.xflags = 0x0\n")
    with pytest.raises(ValueError):
        quota.verify_project(42, 100001)


def test_prepare_freezes_namespace_limits_then_verifies_each_dir(quota, tmp_path, monkeypatch):
    attempt = "01111111-2222-4333-8444-555555555555-" + "a" * 32
    taskroot = tmp_path / "tasks"
    directory = taskroot / attempt
    directory.mkdir(parents=True, mode=0o700)
    (directory / "workspace").mkdir(mode=0o700)
    (directory / "state").mkdir(mode=0o700)
    counters = tmp_path / "counter"
    counters.mkdir()
    monkeypatch.setattr(quota, "COUNTER_ROOT", counters)
    monkeypatch.setattr(quota, "trusted_parents", lambda *_: None)
    monkeypatch.setattr(quota, "protected", lambda *a, **kw: None)
    chowns = []
    monkeypatch.setattr(quota.os, "fchown", lambda fd, uid, gid: chowns.append((fd, uid, gid)))
    monkeypatch.setattr(quota, "check_enforcement", lambda *_: None)
    checked = []
    monkeypatch.setattr(quota, "verify_project", lambda fd, project: checked.append(project))
    commands = []
    def command(mount, text):
        commands.append(text)
        if "report -p -b" in text:
            return "#100000 0 65536 65536 00 [--------]\n"
        if "report -p -i" in text:
            return "#100000 0 1024 1024 00 [--------]\n"
        return ""
    monkeypatch.setattr(quota, "quota_command", command)
    data = {"task_root": str(taskroot), "controller_uid": quota.os.getuid(),
            "controller_gid": quota.os.getgid(), "first_project_id": 100000,
            "quota_bytes": 67_108_864, "quota_inodes": 1024}
    assert quota.prepare(data, attempt, SimpleNamespace(mountpoint=taskroot)) == 100000
    assert "bhard=67108864" in commands[0]
    assert commands[1] == f"project -s -p {directory} 100000"
    assert checked == [100000] * 3
    assert len(chowns) == 8  # root + attempt + two children, restored after scope


def test_prepare_rejects_existing_files_before_assigning_a_quota(quota, tmp_path, monkeypatch):
    attempt = "01111111-2222-4333-8444-555555555555-" + "b" * 32
    root = tmp_path / "tasks"
    target = root / attempt
    target.mkdir(parents=True, mode=0o700)
    (target / "workspace").mkdir(mode=0o700)
    (target / "state").mkdir(mode=0o700)
    (target / "state" / "config.yaml").write_text("already used")
    monkeypatch.setattr(quota, "COUNTER_ROOT", tmp_path)
    monkeypatch.setattr(quota, "trusted_parents", lambda *_: None)
    monkeypatch.setattr(quota, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(quota.os, "fchown", lambda *_: None)
    monkeypatch.setattr(quota, "reserve_project", lambda *_: pytest.fail("must reject before allocating"))
    data = {"task_root": str(root), "controller_uid": quota.os.getuid(), "controller_gid": quota.os.getgid()}
    with pytest.raises(ValueError, match="fresh"):
        quota.prepare(data, attempt, SimpleNamespace(mountpoint=root))
