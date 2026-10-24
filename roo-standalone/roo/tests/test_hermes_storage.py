from types import SimpleNamespace

import pytest

from roo.hermes.storage import inspect_task_storage


def inventory(root, *, device="8:2", fstype="xfs", options="rw,prjquota", subtree="/"):
    return ("20 1 8:1 / / rw - ext4 /dev/vda1 rw\n"
            f"30 20 {device} {subtree} {root} rw - {fstype} /dev/vdb {options}\n")


def facts(*_):
    return SimpleNamespace(f_blocks=1024, f_frsize=4096, f_bavail=512)


def test_dedicated_bounded_quota_volume(tmp_path):
    result = inspect_task_storage(tmp_path, max_filesystem_bytes=8_388_608,
                                  mountinfo=inventory(tmp_path), statvfs=facts)
    assert result.mountpoint == tmp_path
    assert result.total_bytes == 4_194_304
    assert result.project_quotas is True


@pytest.mark.parametrize("change", [
    {"device": "8:1"}, {"fstype": "ext4"}, {"options": "rw"},
    {"options": "rw,pquota,pqnoenforce"}, {"options": "ro,prjquota"},
    {"subtree": "/shared"},
])
def test_reject_unbounded_or_unenforced_volumes(tmp_path, change):
    with pytest.raises(ValueError):
        inspect_task_storage(tmp_path, max_filesystem_bytes=8_388_608,
                             mountinfo=inventory(tmp_path, **change), statvfs=facts)


def test_mounted_volume_is_still_subject_to_capacity_limit(tmp_path):
    with pytest.raises(ValueError, match="capacity"):
        inspect_task_storage(tmp_path, max_filesystem_bytes=1024,
                             mountinfo=inventory(tmp_path), statvfs=facts)


def test_longest_mount_wins_and_cannot_mask_unsafe_child(tmp_path):
    child = tmp_path / "tasks"
    child.mkdir()
    mounts = inventory(tmp_path) + f"31 30 8:2 /subtree {child} rw - xfs /dev/vdb rw,prjquota\n"
    with pytest.raises(ValueError):
        inspect_task_storage(child, max_filesystem_bytes=8_388_608,
                             mountinfo=mounts, statvfs=facts)


def test_symlink_task_root_is_rejected(tmp_path):
    alias = tmp_path / "alias"
    target = tmp_path / "target"
    target.mkdir()
    alias.symlink_to(target)
    with pytest.raises(ValueError):
        inspect_task_storage(alias, max_filesystem_bytes=8_388_608,
                             mountinfo=inventory(tmp_path), statvfs=facts)
