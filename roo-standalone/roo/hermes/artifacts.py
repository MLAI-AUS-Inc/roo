"""Read bounded artifact snapshots without following agent-controlled links."""

import hashlib
import os
import stat
from pathlib import Path

from .types import HermesArtifact


class ArtifactError(ValueError):
    pass


def collect_artifacts(workspace: Path, *, max_bytes: int, max_files: int,
                      secrets: tuple[str, ...] = ()) -> tuple[HermesArtifact, ...]:
    """Copy bytes through no-follow directory descriptors, never reopen by path.

    Only workspace/artifacts is exported. Symlinks, hardlinks, special files and
    oversize trees fail the whole export. Call after execution has been stopped.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    result: list[HermesArtifact] = []
    total = 0
    visited = 0

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal total, visited
        if depth > 12:
            raise ArtifactError("artifact_depth")
        for name in sorted(os.listdir(fd)):
            visited += 1
            if visited > 1024 or name in {".", ".."} or "/" in name:
                raise ArtifactError("artifact_tree")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(result) >= max_files or info.st_size > max_bytes - total:
                    raise ArtifactError("artifact_limit")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    actual = os.fstat(child)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise ArtifactError("artifact_type")
                    chunks = []
                    read = 0
                    while True:
                        chunk = os.read(child, min(65536, max_bytes - total - read + 1))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        read += len(chunk)
                        if total + read > max_bytes:
                            raise ArtifactError("artifact_limit")
                    content = b"".join(chunks)
                finally:
                    os.close(child)
                if any(secret and secret.encode() in content for secret in secrets):
                    raise ArtifactError("artifact_credential")
                total += len(content)
                result.append(HermesArtifact(relative, len(content), hashlib.sha256(content).hexdigest(), content))
            else:
                raise ArtifactError("artifact_type")

    root = os.open(workspace, flags)
    try:
        try:
            export = os.open("artifacts", flags, dir_fd=root)
        except FileNotFoundError:
            return ()
        try:
            walk(export, "", 0)
        finally:
            os.close(export)
    except OSError as exc:
        raise ArtifactError("artifact_access") from exc
    finally:
        os.close(root)
    return tuple(result)
