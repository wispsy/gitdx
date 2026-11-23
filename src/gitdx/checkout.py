"""Reviewable portable checkout plans; mutation is a separate explicit action."""

from dataclasses import dataclass, field, fields
import os
from pathlib import Path
import stat
import unicodedata

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import UnsafePath, check_path, is_reparse_point, read_file
from .index import Index, IndexEntry
from .names import validate_branch
from .object_store import ObjectLimitExceeded
from .objects import Blob, Commit, ObjectId, ObjectType, Tree
from .refs import DirectRef, SymbolicRef, RefConflict
from .working_tree import WorkingTree, WorktreeLimits, portable_path


class CheckoutConflict(GitdxError):
    """Checkout would lose data or its reviewed inputs have changed."""


class CheckoutRecoveryRequired(GitdxError):
    """A retained checkout journal needs explicit recovery."""


@dataclass(frozen=True, slots=True)
class CheckoutLimits:
    max_paths: int = 100_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_bytes: int = 256 * 1024 * 1024
    max_depth: int = 128

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name)<=0 for f in fields(self)):
            raise ValueError("checkout limits must be positive integers")


@dataclass(frozen=True, slots=True)
class FileValue:
    mode: int
    data: bytes = field(repr=False)
    permissions: int = 0


@dataclass(frozen=True, slots=True)
class CheckoutChange:
    path: bytes
    before_mode: int | None
    after_mode: int | None
    before_oid: ObjectId | None
    after_oid: ObjectId | None


@dataclass(frozen=True, slots=True)
class CheckoutPlan:
    git_dir: Path
    destination: str | ObjectId
    unborn: bool
    overwrite_tracked: bool
    head: DirectRef | SymbolicRef
    source_name: str
    source_value: DirectRef | None
    target: ObjectId | None
    target_name: str
    target_value: DirectRef | None
    changes: tuple[CheckoutChange, ...]
    limits: CheckoutLimits
    before: tuple = field(repr=False)
    after: tuple = field(repr=False)
    layout: tuple = field(repr=False)
    old_index: bytes | None = field(repr=False)
    new_index: bytes = field(repr=False)
    config: bytes = field(repr=False)


class _Budget:
    def __init__(self, limits):self.limits,self.total = limits,0

    def add(self, size):
        self.total += size
        if size>self.limits.max_file_bytes or self.total>self.limits.max_bytes:
            raise ObjectLimitExceeded("checkout content byte limit exceeded")


def _key(path):return unicodedata.normalize("NFC", os.fsdecode(path)).casefold()


def _parents(path):
    parts = path.split(b"/")
    return {b"/".join(parts[:i]) for i in range(1,len(parts))}


def _directories(paths):
    result = set()
    for path in paths:result.update(_parents(path))
    return result


def _index_bytes(repo):
    try:return read_file(repo.git_dir, repo.git_dir/"index", repo.index.limits.max_bytes)
    except FileNotFoundError:return None


def _profile(repo):
    if repo.bare:raise UnsupportedFormat("checkout requires a working tree")
    for name in ("checkout-state","MERGE_HEAD","CHERRY_PICK_HEAD","REVERT_HEAD","rebase-merge","rebase-apply","sequencer"):
        path = repo.git_dir/name;check_path(repo.git_dir,path)
        if path.exists():
            if name=="checkout-state":raise CheckoutRecoveryRequired("checkout recovery is pending")
            raise UnsupportedFormat("finish the existing repository operation before checkout")


def _tree(repo, oid, limits, budget):
    if oid is None:return {}
    if oid.format!=repo.object_format:raise InvalidObject("checkout commit format mismatch")
    raw = repo.objects.read(oid)
    if raw.kind is not ObjectType.COMMIT:raise InvalidObject("checkout target must be a commit")
    budget.add(len(raw.data));commit = Commit.from_bytes(raw.data,repo.object_format)
    result = {};stack = [(b"",commit.tree,0)];names = {};count = 0
    while stack:
        base,tree_id,depth = stack.pop()
        if depth>limits.max_depth:raise ObjectLimitExceeded("checkout tree depth exceeded")
        raw = repo.objects.read(tree_id)
        if raw.kind is not ObjectType.TREE:raise InvalidObject("checkout directory must name a tree")
        budget.add(len(raw.data))
        for entry in Tree.from_bytes(raw.data,repo.object_format).entries:
            path = base+entry.name;portable_path(path);count += 1
            if count>limits.max_paths:raise ObjectLimitExceeded("checkout tree entry limit exceeded")
            key = _key(path)
            if key in names and names[key]!=path:raise UnsafePath("checkout tree has case/normalization aliases")
            names[key]=path
            if entry.mode==0o40000:stack.append((path+b"/",entry.oid,depth+1))
            elif entry.mode==0o160000:raise UnsupportedFormat("submodule checkout is unsupported")
            else:result[path]=(entry.oid,entry.mode)
    return result


def _capture(root, raw, budget):
    """Read a leaf without traversing symlink or non-directory parents."""
    path = root
    parts = os.fsdecode(raw).split("/")
    for part in parts[:-1]:
        path /= part
        try:info = path.lstat()
        except FileNotFoundError:return None
        if is_reparse_point(info) and not stat.S_ISLNK(info.st_mode):raise UnsafePath("checkout parent is a reparse point")
        if not stat.S_ISDIR(info.st_mode):return None
    path /= parts[-1]
    try:before = path.lstat()
    except (FileNotFoundError, NotADirectoryError):return None
    if is_reparse_point(before) and not stat.S_ISLNK(before.st_mode):raise UnsafePath("checkout path is a reparse point")
    permissions = stat.S_IMODE(before.st_mode)
    if os.name=="nt":
        if not permissions&stat.S_IWUSR:raise UnsupportedFormat("Windows checkout requires writable affected paths")
        permissions = 0o777 if stat.S_ISDIR(before.st_mode) else 0o666
    if stat.S_ISDIR(before.st_mode):return FileValue(0o40000,b"",permissions)
    if stat.S_ISLNK(before.st_mode):
        data = os.fsencode(os.readlink(path));mode = 0o120000;permissions = 0
    elif stat.S_ISREG(before.st_mode):
        fd = os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
        with os.fdopen(fd,"rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(before.st_dev,before.st_ino):raise CheckoutConflict("working file changed during planning")
            data = stream.read(budget.limits.max_file_bytes+1)
        mode = 0o100755 if os.name!="nt" and before.st_mode&stat.S_IXUSR else 0o100644
    else:raise UnsupportedFormat("checkout encountered a non-regular working file")
    after = path.lstat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_mode)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_mode):
        raise CheckoutConflict("working file changed during planning")
    budget.add(len(data))
    return FileValue(mode,data,permissions)


def _value_id(value, format):
    return Blob(value.data).to_raw().oid(format) if value and value.mode!=0o40000 else None


def plan_checkout(repo, destination, *, unborn=False, overwrite_tracked=False, limits=None):
    limits = limits or CheckoutLimits();_profile(repo)
    if type(unborn) is not bool or type(overwrite_tracked) is not bool:raise TypeError("checkout policies must be explicit booleans")
    budget = _Budget(limits)
    work = WorkingTree(repo,limits=WorktreeLimits(limits.max_paths,limits.max_file_bytes,limits.max_bytes))
    if os.name=="nt" and work.filemode:raise UnsupportedFormat("Windows checkout requires core.filemode=false")
    config = read_file(repo.git_dir,repo.git_dir/"config",1024*1024)
    refs = repo.refs;head = refs.read("HEAD");source = refs.resolve()
    if not isinstance(head,(DirectRef,SymbolicRef)):raise InvalidObject("checkout requires an existing HEAD")
    if isinstance(head,SymbolicRef) and (source.symbolic_chain!=("HEAD",) or not head.target.startswith("refs/heads/")):
        raise UnsupportedFormat("checkout requires detached HEAD or a direct branch symbolic HEAD")
    if isinstance(head,SymbolicRef) and source.name!=head.target:raise RefConflict("HEAD changed while planning checkout")
    source_value = refs.read(source.name)
    if source_value!=(DirectRef(source.oid) if source.oid else None):raise RefConflict("source branch changed while planning")
    if isinstance(destination,ObjectId):
        if unborn:raise ValueError("a detached checkout cannot be unborn")
        target,target_name,target_value = destination,"HEAD",None
    else:
        validate_branch(destination);target_name = "refs/heads/"+destination
        target_value = refs.read(target_name)
        if isinstance(target_value,SymbolicRef):raise UnsupportedFormat("checkout target branch must be direct")
        if unborn and target_value is not None:raise CheckoutConflict("unborn destination must not exist")
        if not unborn and target_value is None:raise CheckoutConflict("checkout target branch does not exist")
        target = target_value.oid if target_value else None
    old_index = _index_bytes(repo);index = repo.index.read()
    if any(e.stage or e.mode==0o160000 for e in index.entries):raise UnsupportedFormat("unmerged/submodule indexes cannot be checked out")
    current = _tree(repo,source.oid,limits,budget);wanted = _tree(repo,target,limits,budget)
    indexed = {e.path:(e.oid,e.mode) for e in index.entries}
    if not overwrite_tracked and indexed!=current:raise CheckoutConflict("staged changes require explicit tracked overwrite")
    tracked = set(current)|set(indexed);nodes = work._paths(index,force=True)
    names = {_key(path):path for path in nodes}
    wanted_dirs = _directories(wanted)
    managed_dirs = _directories(tracked)
    for path in (*wanted,*wanted_dirs):
        if _key(path) in names and names[_key(path)]!=path:raise UnsafePath("checkout target aliases an existing path")
        names[_key(path)]=path
    protected_dirs = set()
    for path,(_,info,_) in nodes.items():
        directory = stat.S_ISDIR(info.st_mode)
        if (not directory and path not in tracked) or (directory and path not in managed_dirs):
            protected_dirs.update(_parents(path))
            if directory:protected_dirs.add(path)
            if path in wanted or any(parent in wanted for parent in _parents(path)) or (not directory and path in wanted_dirs):
                raise CheckoutConflict("checkout would overwrite untracked content")
    affected = tracked|set(wanted)|managed_dirs|wanted_dirs
    if len(affected)>limits.max_paths:raise ObjectLimitExceeded("checkout path/parent limit exceeded")
    before = {p:_capture(repo.work_tree,p,budget) for p in sorted(affected)}
    if not overwrite_tracked:
        for path,(oid,mode) in indexed.items():
            value = before[path]
            physical_mode = value.mode if value else None
            if value and not work.filemode and physical_mode in (0o100644,0o100755):physical_mode = mode
            if value and not work.symlinks and mode==0o120000 and physical_mode in (0o100644,0o100755):physical_mode=mode
            if physical_mode!=mode or _value_id(value,repo.object_format)!=oid:
                raise CheckoutConflict("modified tracked files require explicit overwrite")
    after = dict(before)
    for path in tracked-set(wanted):
        if before[path] and before[path].mode==0o40000:raise CheckoutConflict("a tracked path was replaced by an untracked directory")
        after[path]=None
    for path in managed_dirs-wanted_dirs:
        if before[path] and before[path].mode==0o40000 and path not in protected_dirs:after[path]=None
    for path in wanted_dirs:
        value = before[path]
        after[path] = value if value and value.mode==0o40000 else FileValue(0o40000,b"",0o777 if os.name=="nt" else 0o700)
    for path,(oid,mode) in wanted.items():
        raw = repo.objects.read(oid)
        if raw.kind is not ObjectType.BLOB:raise InvalidObject("checkout file must name a blob")
        budget.add(len(raw.data))
        if path.split(b"/")[-1].lower()==b".gitattributes" and any(l.strip() and not l.lstrip().startswith(b"#") for l in raw.data.splitlines()):
            raise UnsupportedFormat("checkout attribute transformations are unsupported")
        previous = before[path]
        if mode==0o120000 and work.symlinks:
            if os.name=="nt":raise UnsupportedFormat("Windows symlink checkout requires core.symlinks=false")
            if not raw.data or b"\0" in raw.data or len(raw.data)>1023:raise UnsupportedFormat("symlink value is outside the portable checkout profile")
            value = FileValue(mode,raw.data)
        else:
            permissions = (previous.permissions&0o777) if previous and previous.mode in (0o100644,0o100755) else 0o600
            executable = mode==0o100755
            if not work.filemode and previous and previous.mode in (0o100644,0o100755):executable=previous.mode==0o100755
            permissions = (permissions|stat.S_IXUSR) if executable else (permissions&~0o111)
            if os.name=="nt":permissions=0o666;executable=False
            value = FileValue(0o100755 if executable else 0o100644,raw.data,permissions)
        after[path]=value
    changes = tuple(CheckoutChange(p,before[p].mode if before[p] else None,after[p].mode if after[p] else None,_value_id(before[p],repo.object_format),_value_id(after[p],repo.object_format)) for p in sorted(affected) if before[p]!=after[p])
    new_index = Index(tuple(IndexEntry(path,oid,mode) for path,(oid,mode) in wanted.items()),repo.object_format).encode(repo.index.limits)
    if old_index!=_index_bytes(repo) or head!=refs.read("HEAD") or source_value!=refs.read(source.name) or (target_name!="HEAD" and target_value!=refs.read(target_name)):
        raise CheckoutConflict("repository inputs changed during planning")
    return CheckoutPlan(repo.git_dir,destination,unborn,overwrite_tracked,head,source.name,source_value,target,target_name,target_value,changes,limits,
                        tuple(sorted(before.items())),tuple(sorted(after.items())),tuple(sorted((p,stat.S_IFMT(n[1].st_mode)) for p,n in nodes.items())),old_index,new_index,config)
