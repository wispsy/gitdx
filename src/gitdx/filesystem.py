"""Static path checks inside owner-controlled storage directories."""

from pathlib import Path
import os
import stat
import tempfile

from .errors import GitdxError
from .confined_fs import active


class UnsafePath(GitdxError, ValueError):
    """A storage path crosses an unsupported filesystem boundary."""


def is_reparse_point(info) -> bool:
    return bool(getattr(info,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400))


def check_path(root: Path, target: Path) -> None:
    """Reject existing symlinks, including the root itself; do not resolve them.

    Callers must own the directory. This is not a sandbox against an adversary
    concurrently replacing path components, or a power-loss durability promise.
    """
    relative = target.relative_to(root)
    if any(part in ('.','..') for part in relative.parts):raise UnsafePath('storage paths cannot contain traversal components')
    scope=active()
    if scope is not None:
        scope.parts(root);scope.parts(target);scope.unchanged()
        try:
            info=scope.info(target)
            scope._owned(info,directory=stat.S_ISDIR(info.st_mode))
        except FileNotFoundError:pass
        return
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            info = current.lstat();mode = info.st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or is_reparse_point(info):
            raise UnsafePath("symlinks and reparse points are not supported inside repository storage")
        if current != target and not stat.S_ISDIR(mode):
            raise UnsafePath("repository storage parent is not a directory")


def read_file(root: Path, path: Path, limit: int, *, limit_error: type[Exception] = UnsafePath) -> bytes:
    relative=path.relative_to(root)
    if any(part in ('.','..') for part in relative.parts):raise UnsafePath('storage paths cannot contain traversal components')
    scope=active()
    if scope is not None:
        scope.parts(root);scope.unchanged()
        return scope.read(path,limit,limit_error)
    check_path(root, path)
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise UnsafePath("repository metadata must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise limit_error("repository metadata exceeds its configured byte limit")
    return data


def replace_file(root: Path, path: Path, data: bytes) -> None:
    """Publish a complete file while the caller retains its separate lock."""
    check_path(root,path)
    scope=active()
    if scope is not None:
        scope.parts(root);scope.unchanged();scope.write(path,data)
        return
    fd, name = tempfile.mkstemp(prefix="tmp_meta_", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        check_path(root, path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def storage_exists(path):
    scope=active()
    return scope.exists(path) if scope is not None else path.exists()


def storage_stat(path):
    scope=active()
    return scope.info(path) if scope is not None else path.stat()


def storage_is_dir(path):
    try:return stat.S_ISDIR(storage_stat(path).st_mode)
    except FileNotFoundError:return False


def storage_names(path):
    scope=active()
    return scope.names(path) if scope is not None else (p.name for p in path.iterdir())


def storage_unlink(path,*,missing_ok=False):
    scope=active()
    if scope is not None:scope.unlink(path,missing_ok=missing_ok)
    else:path.unlink(missing_ok=missing_ok)


def storage_mkdtemp(path,prefix):
    scope=active()
    return scope.mkdtemp(path,prefix) if scope is not None else Path(tempfile.mkdtemp(prefix=prefix,dir=path))


def storage_rename(source,target):
    scope=active()
    if scope is not None:scope.rename(source,target)
    else:os.rename(source,target)


def storage_remove_tree(path,*,identity=None):
    scope=active()
    if scope is not None:
        with scope.cleanup():scope.remove_tree(path,identity=identity)
    else:
        import shutil
        if identity is not None:
            info=path.stat()
            if (info.st_dev,info.st_ino)!=tuple(identity):raise UnsafePath('cleanup ownership identity changed')
        shutil.rmtree(path)


def storage_write_exclusive(path,data):
    scope=active()
    if scope is not None:scope.write(path,data,exclusive=True)
    else:
        with path.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
