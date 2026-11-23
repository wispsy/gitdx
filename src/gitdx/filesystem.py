"""Static path checks inside owner-controlled storage directories."""

from pathlib import Path
import stat

from .errors import GitdxError


class UnsafePath(GitdxError, ValueError):
    """A storage path crosses an unsupported filesystem boundary."""


def check_path(root: Path, target: Path) -> None:
    """Reject existing symlinks, including the root itself; do not resolve them.

    Callers must own the directory. This is not a sandbox against an adversary
    concurrently replacing path components, or a power-loss durability promise.
    """
    relative = target.relative_to(root)
    if any(part in (".", "..") for part in relative.parts):
        raise UnsafePath("storage paths cannot contain traversal components")
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise UnsafePath("symlinks are not supported inside repository storage")
        if current != target and not stat.S_ISDIR(mode):
            raise UnsafePath("repository storage parent is not a directory")


def read_file(root: Path, path: Path, limit: int, *, limit_error: type[Exception] = UnsafePath) -> bytes:
    check_path(root, path)
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise UnsafePath("repository metadata must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise limit_error("repository metadata exceeds its configured byte limit")
    return data
