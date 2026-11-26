"""Explicit repository lifecycle and conservative on-disk format detection."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from .config import RepositoryConfig, parse_config
from .errors import GitdxError, UnsupportedFormat
from .filesystem import check_path, read_file
from .names import InvalidRef, validate_branch, validate_refname
from .object_store import StoreLimits
from .pack import PackLimits, RepositoryObjectStore
from .objects import ObjectFormat, ObjectId
from .refs import FilesRefStore
from .index import IndexFile
from .repository_profile import check_repository_features


class RepositoryNotFound(GitdxError, LookupError):
    """The selected path is not a supported Git repository."""


@dataclass(frozen=True, slots=True)
class Repository:
    git_dir: Path
    work_tree: Path | None
    config: RepositoryConfig
    objects: RepositoryObjectStore

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

    def add(self, *paths, force=False):
        from .working_tree import WorkingTree
        return WorkingTree(self).add(paths,force=force)

    def remove(self, *paths):
        from .working_tree import WorkingTree
        return WorkingTree(self).remove(paths)

    def status(self):
        from .working_tree import WorkingTree
        return WorkingTree(self).status()

    def commit(self, *, author, committer, message, expected_head, parents=None, limits=None):
        from .history import create_commit
        return create_commit(self, author=author, committer=committer, message=message,
                             expected_head=expected_head, parents=parents, limits=limits)

    def log(self, start=None, *, max_count=100, limits=None):
        from .history import log
        return log(self, start, max_count=max_count, limits=limits)

    def walk_history(self, starts, *, limits=None):
        from .history import walk_history
        return walk_history(self, starts, limits=limits)

    def diff(self, old_tree, new_tree, **options):
        from .diff import diff_trees
        return diff_trees(self, old_tree, new_tree, **options)

    def resolve(self, revision, *, limits=None):
        from .revisions import resolve_revision
        return resolve_revision(self, revision, limits=limits)

    def is_ancestor(self, ancestor, descendant, *, limits=None):
        from .ancestry import is_ancestor
        return is_ancestor(self, ancestor, descendant, limits=limits)

    def merge_bases(self, left, right, *, limits=None):
        from .ancestry import merge_bases
        return merge_bases(self, left, right, limits=limits)

    def plan_merge(self, revision, *, base=None, limits=None):
        from .merge import plan_merge
        return plan_merge(self,revision,base=base,limits=limits)

    def apply_merge(self, plan):
        from .merge import apply_merge
        return apply_merge(self,plan)

    def finish_merge(self, **options):
        from .merge import finish_merge
        return finish_merge(self,**options)

    def plan_abort_merge(self, *, discard_tracked=False):
        from .merge import plan_abort_merge
        return plan_abort_merge(self,discard_tracked=discard_tracked)

    def abort_merge(self, plan):
        from .merge import abort_merge
        return abort_merge(self,plan)

    def recover_merge(self):
        from .merge import recover_merge
        return recover_merge(self)

    def plan_reset(self, revision, **options):
        from .reset_restore import plan_reset
        return plan_reset(self,revision,**options)

    def apply_reset(self, plan):
        from .reset_restore import apply_reset
        return apply_reset(self,plan)

    def plan_restore(self, *paths, **options):
        from .reset_restore import plan_restore
        return plan_restore(self,paths,**options)

    def apply_restore(self, plan):
        from .reset_restore import apply_restore
        return apply_restore(self,plan)

    def recover_operation(self):
        from .worktree_transaction import recover_mutation
        return recover_mutation(self)

    def branches(self, *, limits=None):
        from .history import list_refs
        return list_refs(self, "refs/heads/", limits=limits)

    def tags(self, *, limits=None):
        from .history import list_refs
        return list_refs(self, "refs/tags/", limits=limits)

    def pack(self, roots, *, limits=None):
        from .pack_writer import write_pack
        return write_pack(self.objects, roots, limits=limits)

    def receive(self, commands, pack=b'', **options):
        from .receive import receive
        return receive(self,commands,pack,**options)

    def plan_checkout(self, destination, *, unborn=False, overwrite_tracked=False, limits=None):
        from .checkout import plan_checkout
        return plan_checkout(self, destination, unborn=unborn, overwrite_tracked=overwrite_tracked, limits=limits)

    def apply_checkout(self, plan):
        from .checkout_transaction import apply_checkout
        return apply_checkout(self, plan)

    def recover_checkout(self):
        from .checkout_transaction import recover_checkout
        return recover_checkout(self)

    def fetch(self, transport, **options):
        from .transfers import fetch
        return fetch(self, transport, **options)

    def push(self, transport, updates, **options):
        from .push import push
        return push(self, transport, updates, **options)

    @classmethod
    def clone(cls, transport, path, **options):
        from .transfers import clone
        return clone(transport, path, **options)

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
    def open(cls, path: str | os.PathLike[str], *, limits: StoreLimits | None = None, pack_limits: PackLimits | None = None) -> Repository:
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
        check_path(git_dir, git_dir / "HEAD")
        head = read_file(git_dir, git_dir / "HEAD", 4096).removesuffix(b"\n")
        config = parse_config(read_file(git_dir, git_dir / "config", 1024 * 1024))
        check_repository_features(git_dir,config)
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
        objects = RepositoryObjectStore(git_dir / "objects", config.object_format, limits=limits, pack_limits=pack_limits)
        return cls(git_dir, work_tree, config, objects)

    @classmethod
    def discover(cls, start: str | os.PathLike[str] = ".", *, limits: StoreLimits | None = None, pack_limits: PackLimits | None = None) -> Repository:
        path = Path(start).absolute()
        if path.is_file():
            path = path.parent
        for candidate in (path, *path.parents):
            try:
                return cls.open(candidate, limits=limits, pack_limits=pack_limits)
            except RepositoryNotFound:
                continue
        raise RepositoryNotFound("no repository found in the selected directory or its parents")
