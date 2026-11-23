"""Explicit repository lifecycle and conservative on-disk format detection."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from .config import RepositoryConfig, parse_config
from .errors import GitdxError, UnsupportedFormat
from .filesystem import check_path, read_file
from .names import InvalidRef, validate_branch, validate_refname
from .object_store import LooseObjectStore, StoreLimits
from .objects import ObjectFormat, ObjectId
from .refs import FilesRefStore
from .index import IndexFile


class RepositoryNotFound(GitdxError, LookupError):
    """The selected path is not a supported Git repository."""


@dataclass(frozen=True, slots=True)
class Repository:
    git_dir: Path
    work_tree: Path | None
    config: RepositoryConfig
    objects: LooseObjectStore

    @property
    def bare(self) -> bool:
        return self.config.bare

    @property
    def object_format(self) -> ObjectFormat:
        return self.config.object_format

    @property
    def refs(self) -> FilesRefStore:
        return FilesRefStore(self.git_dir, self.objects)

    @property
    def index(self) -> IndexFile:
        if self.bare:
            raise UnsupportedFormat("bare repositories have no working-tree index")
        return IndexFile(self.git_dir, self.object_format)

    @classmethod
    def init(cls, path: str | os.PathLike[str], *, bare: bool = False,
             object_format: ObjectFormat = ObjectFormat.SHA1, initial_branch: str = "main") -> Repository:
        format = ObjectFormat(object_format)
        validate_branch(initial_branch)
        root = Path(path).absolute()
        check_path(root, root)
        if bare and root.exists() and any(root.iterdir()):
            raise FileExistsError("bare repository initialization requires an empty directory")
        root.mkdir(parents=True, exist_ok=True)
        git_dir = root if bare else root / ".git"
        check_path(root, git_dir)
        if not bare:
            git_dir.mkdir()  # Exclusive: never reinitialize or replace existing metadata.
        created = []
        try:
            for name in ("objects", "refs"):
                directory = git_dir / name
                directory.mkdir()
                created.append(directory)
            (git_dir / "objects" / "info").mkdir()
            (git_dir / "objects" / "pack").mkdir()
            (git_dir / "refs" / "heads").mkdir()
            (git_dir / "refs" / "tags").mkdir()
            config = f"[core]\n\trepositoryformatversion = {1 if format is ObjectFormat.SHA256 else 0}\n\tfilemode = {'false' if os.name == 'nt' else 'true'}\n\tbare = {'true' if bare else 'false'}\n"
            if not bare:
                config += "\tlogallrefupdates = true\n"
            if format is ObjectFormat.SHA256:
                config += "[extensions]\n\tobjectformat = sha256\n"
            for name, data in (("config", config.encode("utf-8")),
                               ("HEAD", f"ref: refs/heads/{initial_branch}\n".encode("utf-8"))):
                target = git_dir / name
                with target.open("xb") as stream:
                    created.append(target)
                    stream.write(data)
            return cls.open(root)
        except BaseException:
            for target in reversed(created):
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            if not bare:
                git_dir.rmdir()
            raise

    @classmethod
    def open(cls, path: str | os.PathLike[str], *, limits: StoreLimits | None = None) -> Repository:
        root = Path(path).absolute()
        check_path(root, root)
        if not root.is_dir():
            raise RepositoryNotFound("repository path must be an existing directory")
        marker = root / ".git"
        check_path(root, marker)
        if marker.is_file():
            raise UnsupportedFormat("gitfiles, linked worktrees and submodule worktrees are unsupported")
        if marker.exists() and not marker.is_dir():
            raise UnsupportedFormat("repository marker must be a supported directory")
        git_dir = marker if marker.is_dir() else root
        check_path(root, git_dir / "config")
        if not (git_dir / "config").is_file() or not (git_dir / "HEAD").exists():
            bare_hint = (git_dir / "objects").exists() and (
                (git_dir / "HEAD").exists() or (git_dir / "config").exists())
            if marker.is_dir() or root.name == ".git" or bare_hint:
                raise UnsupportedFormat("incomplete repository metadata; refusing parent discovery")
            raise RepositoryNotFound("no repository metadata at the selected path")
        for name in ("commondir", "shallow", "objects/info/alternates", "objects/info/http-alternates"):
            target = git_dir / name
            check_path(git_dir, target)
            if target.exists():
                raise UnsupportedFormat(f"repository feature {name} is not supported yet")
        check_path(git_dir, git_dir / "HEAD")
        head = read_file(git_dir, git_dir / "HEAD", 4096).removesuffix(b"\n")
        config = parse_config(read_file(git_dir, git_dir / "config", 1024 * 1024))
        if head.startswith(b"ref: "):
            try:
                validate_refname(head[5:].decode("utf-8"))
            except UnicodeError as error:
                raise InvalidRef("symbolic HEAD must contain a valid UTF-8 ref name") from error
        else:
            try:
                ObjectId(head.decode("ascii"), config.object_format)
            except UnicodeError as error:
                raise InvalidRef("detached HEAD must contain a full object ID") from error
        if git_dir == root:
            if config.bare:
                work_tree = None
            elif root.name == ".git":
                work_tree = root.parent
            else:
                raise UnsupportedFormat("nonstandard worktree layouts are not supported yet")
        else:
            if config.bare:
                raise UnsupportedFormat("bare config cannot be used with an attached worktree")
            work_tree = root
        objects = LooseObjectStore(git_dir / "objects", config.object_format, limits=limits)
        return cls(git_dir, work_tree, config, objects)

    @classmethod
    def discover(cls, start: str | os.PathLike[str] = ".", *, limits: StoreLimits | None = None) -> Repository:
        path = Path(start).absolute()
        if path.is_file():
            path = path.parent
        for candidate in (path, *path.parents):
            try:
                return cls.open(candidate, limits=limits)
            except RepositoryNotFound:
                continue
        raise RepositoryNotFound("no repository found in the selected directory or its parents")
