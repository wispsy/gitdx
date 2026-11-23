"""Explicit staging and deterministic index/HEAD/worktree status."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import unicodedata

from .errors import GitdxError, UnsupportedFormat
from .filesystem import UnsafePath, check_path, read_file
from .ignore import parse_ignore
from .index import IndexEntry, IndexStat
from .objects import Blob, Commit, ObjectType, Tree
from .config import parse_config


class IgnoredPath(GitdxError, ValueError):
    """An explicitly requested untracked path is ignored."""


@dataclass(frozen=True, slots=True)
class WorktreeLimits:
    max_paths: int = 100_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if any(type(n) is not int or n <= 0 for n in (self.max_paths,self.max_file_bytes,self.max_total_bytes)):
            raise ValueError("worktree limits must be positive integers")


@dataclass(frozen=True, slots=True)
class StatusEntry:
    path: bytes
    index: str
    worktree: str

    @property
    def xy(self):return self.index + self.worktree


def portable_path(path) -> bytes:
    value = os.fsencode(path)
    if value == b".":return b""
    if not value or value.startswith(b"/") or len(value) > 4096:raise UnsafePath("expected a bounded relative working-tree path")
    for part in value.split(b"/"):
        normalized = unicodedata.normalize("NFC", os.fsdecode(part)).casefold()
        device = normalized.split(".",1)[0].upper()
        if part in (b"",b".",b"..") or part.endswith((b".",b" ")) or normalized in (".git","git~1",".git~1"):
            raise UnsafePath("working-tree path contains reserved metadata or traversal components")
        if any(c < 32 or c == 127 or c in b'\\<>:"|?*' for c in part) or device in {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}:
            raise UnsafePath("working-tree path is outside the portable supported subset")
    return value


class WorkingTree:
    def __init__(self, repo, *, limits: WorktreeLimits | None = None):
        if repo.bare:raise UnsupportedFormat("bare repositories have no working tree")
        self.repo, self.root, self.limits = repo, repo.work_tree, limits or WorktreeLimits()
        self._rules = {};self._rule_count = 0;self._bytes = 0
        self._policy()

    def _policy(self):
        raw_config = read_file(self.repo.git_dir,self.repo.git_dir/"config",1024*1024)
        current = parse_config(raw_config)
        if current.object_format != self.repo.object_format:raise UnsupportedFormat("repository object format changed")
        config = raw_config.lower()
        if re.search(rb"\[filter(?:\s|\])|\battributesfile\s*=",config):raise UnsupportedFormat("external filters/attribute files are unsupported")
        match = re.findall(rb"(?m)^\s*autocrlf\s*=\s*([^#;\r\n]+)",config)
        if any(v.strip() not in (b"false",b"no",b"off",b"0",b"") for v in match):raise UnsupportedFormat("automatic line-ending conversion is unsupported")
        self.filemode = current.filemode
        self.symlinks = current.symlinks

    def _file(self, relative):return self.root / os.fsdecode(relative)

    def _metadata(self, path):
        try:return read_file(self.root,path,1024*1024)
        except FileNotFoundError:return b""

    def _local_rules(self, base):
        if base not in self._rules:
            path = self._file(base)/".gitignore"
            rules = parse_ignore(self._metadata(path),base)
            if not base:
                rules = parse_ignore(read_file(self.repo.git_dir,self.repo.git_dir/"info/exclude",1024*1024) if (self.repo.git_dir/"info/exclude").exists() else b"") + rules
            self._rule_count += len(rules)
            if self._rule_count > 10_000:raise UnsupportedFormat("ignore rule count exceeds the supported limit")
            self._rules[base] = rules
        return self._rules[base]

    def ignored(self, path, directory=False):
        parts = path.split(b"/");rules = []
        for i in range(len(parts)):
            base = b"/".join(parts[:i]);rules.extend(self._local_rules(base))
            current = b"/".join(parts[:i+1]);is_dir = i < len(parts)-1 or directory
            ignored = False
            for rule in rules:
                if rule.matches(current,is_dir):ignored = not rule.negate
            if ignored:return True
        return False

    def _paths(self, index, *, force=False):
        tracked = {e.path for e in index.entries};folded = {};nodes = {};stack = [(b"",self.root)]
        needed = set()
        for raw in tracked:
            parts = raw.split(b"/")
            needed.update(b"/".join(parts[:i]) for i in range(1,len(parts)))
            if len(needed)>self.limits.max_paths:raise UnsupportedFormat("tracked directory count exceeds its limit")
        def register(raw):
            portable_path(raw)
            key = unicodedata.normalize("NFC",os.fsdecode(raw)).casefold()
            if key in folded and folded[key] != raw:raise UnsafePath("case/normalization collision in working tree or index")
            folded[key] = raw
        for raw in tracked:register(raw)
        while stack:
            base, directory = stack.pop()
            check_path(self.root,directory)
            if base and (directory/".git").exists():raise UnsupportedFormat("nested repositories/submodules are unsupported")
            attributes = self._metadata(directory/".gitattributes")
            if any(line.strip() and not line.lstrip().startswith(b"#") for line in attributes.splitlines()):raise UnsupportedFormat("working-tree attribute transformations are unsupported")
            with os.scandir(directory) as iterator:children = sorted(iterator,key=lambda e:os.fsencode(e.name))
            for child in children:
                if not base and child.name == ".git":continue
                raw = (base+b"/" if base else b"") + os.fsencode(child.name);register(raw)
                if len(nodes) >= self.limits.max_paths:raise UnsupportedFormat("working-tree path count exceeds its limit")
                # Windows DirEntry caches omit file identifiers used below.
                info = Path(child.path).lstat()
                is_dir = stat.S_ISDIR(info.st_mode)
                ignored = self.ignored(raw,is_dir)
                nodes[raw] = (Path(child.path),info,ignored)
                if is_dir and (force or not ignored or raw in needed):
                    stack.append((raw,Path(child.path)))
        return nodes

    def _read(self, raw, node, previous=None):
        path, before, _ = node
        check_path(self.root,path.parent)
        if stat.S_ISLNK(before.st_mode):
            data = os.fsencode(os.readlink(path));mode = 0o120000
        elif stat.S_ISREG(before.st_mode):
            fd = os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
            with os.fdopen(fd,"rb") as stream:
                if (os.fstat(stream.fileno()).st_dev,os.fstat(stream.fileno()).st_ino) != (before.st_dev,before.st_ino):raise UnsafePath("working file changed during staging")
                data = stream.read(self.limits.max_file_bytes+1)
            mode = (0o100755 if before.st_mode & stat.S_IXUSR else 0o100644) if self.filemode else (previous.mode if previous and previous.mode in (0o100644,0o100755) else 0o100644)
            if previous and previous.mode == 0o120000 and not self.symlinks:mode = 0o120000
        else:raise UnsupportedFormat("only regular files and symlink values can be staged")
        after = path.lstat()
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise UnsafePath("working file changed during staging")
        self._bytes += len(data)
        if len(data)>self.limits.max_file_bytes or self._bytes>self.limits.max_total_bytes:raise UnsupportedFormat("working-tree content exceeds its byte budget")
        values = IndexStat((before.st_ctime_ns//10**9)&0xffffffff,before.st_ctime_ns%10**9,(before.st_mtime_ns//10**9)&0xffffffff,before.st_mtime_ns%10**9,before.st_dev&0xffffffff,before.st_ino&0xffffffff,before.st_uid&0xffffffff,before.st_gid&0xffffffff,before.st_size&0xffffffff)
        return Blob(data).to_raw(),mode,values

    def _selected(self, path, selectors):return any(not s or path == s or path.startswith(s+b"/") for s in selectors)

    def add(self, paths, *, force=False):
        selectors = tuple(portable_path(p) for p in paths)
        if not selectors:raise ValueError("at least one explicit path is required")
        with self.repo.index.transaction() as transaction:
            index = transaction.index;nodes = self._paths(index,force=force)
            tracked = {e.path for e in index.entries}
            previous_entries = {e.path:e for e in index.entries if e.stage==0}
            for selected in selectors:
                if selected:check_path(self.root,self._file(selected).parent)
                if selected and selected not in nodes and not any(p == selected or p.startswith(selected+b"/") for p in tracked):raise FileNotFoundError("selected path does not exist and is not tracked")
                if selected in nodes and nodes[selected][2] and not any(p==selected or p.startswith(selected+b"/") for p in tracked) and not force:raise IgnoredPath("explicit untracked path is ignored; select force to stage it")
            result = [e for e in index.entries if not self._selected(e.path,selectors)]
            staged = set()
            for raw,node in nodes.items():
                if not self._selected(raw,selectors) or stat.S_ISDIR(node[1].st_mode):continue
                if node[2] and raw not in tracked and not force:continue
                previous = previous_entries.get(raw)
                if previous and previous.mode == 0o160000:raise UnsupportedFormat("submodule staging is unsupported")
                obj,mode,info = self._read(raw,node,previous)
                result.append(IndexEntry(raw,self.repo.objects.write(obj),mode,stat=info));staged.add(raw)
            staged.update(e.path for e in index.entries if self._selected(e.path,selectors))
            transaction.replace(result);transaction.commit()
            return tuple(sorted(staged))

    def remove(self, paths):
        selectors = tuple(portable_path(p) for p in paths)
        if not selectors:raise ValueError("at least one explicit path is required")
        with self.repo.index.transaction() as tx:
            removed = tuple(sorted({e.path for e in tx.index.entries if self._selected(e.path,selectors)}))
            if not removed:raise FileNotFoundError("no tracked paths match the selection")
            tx.replace([e for e in tx.index.entries if not self._selected(e.path,selectors)]);tx.commit()
            return removed

    def _head(self):
        oid = self.repo.refs.resolve().oid
        if oid is None:return {}
        raw = self.repo.objects.read(oid)
        if raw.kind is not ObjectType.COMMIT:raise UnsupportedFormat("HEAD is not a commit")
        tree = Commit.from_bytes(raw.data,self.repo.object_format).tree
        result = {};stack = [(b"",tree,0)]
        while stack:
            base,oid,depth = stack.pop()
            if depth>128:raise UnsupportedFormat("HEAD tree depth exceeds its limit")
            raw = self.repo.objects.read(oid)
            if raw.kind is not ObjectType.TREE:raise UnsupportedFormat("HEAD tree references a non-tree")
            for entry in Tree.from_bytes(raw.data,self.repo.object_format).entries:
                path = (base+b"/" if base else b"")+entry.name;portable_path(path)
                if entry.mode==0o40000:stack.append((path,entry.oid,depth+1))
                else:result[path]=(entry.oid,entry.mode)
                if len(result)+len(stack)>self.limits.max_paths:raise UnsupportedFormat("HEAD tree exceeds its entry limit")
        return result

    def status(self):
        index = self.repo.index.read();head = self._head();nodes = self._paths(index)
        tracked = {e.path for e in index.entries};result = []
        groups = {}
        for e in index.entries:groups.setdefault(e.path,[]).append(e)
        def change(old,new):
            if old is None:return "A" if new is not None else " "
            if new is None:return "D"
            if old==new:return " "
            return "T" if old[1]&0o170000 != new[1]&0o170000 else "M"
        for path in sorted(set(head)|tracked):
            entries = groups.get(path,[])
            if any(e.stage for e in entries):
                xy = {frozenset({1}):"DD",frozenset({2}):"AU",frozenset({3}):"UA",frozenset({1,2}):"UD",frozenset({1,3}):"DU",frozenset({2,3}):"AA",frozenset({1,2,3}):"UU"}[frozenset(e.stage for e in entries)]
                result.append(StatusEntry(path,*xy));continue
            entry = entries[0] if entries else None
            if entry and entry.mode==0o160000:raise UnsupportedFormat("submodule status is unsupported")
            staged = change(head.get(path),(entry.oid,entry.mode) if entry else None);working = " "
            if entry and not entry.assume_valid:
                node = nodes.get(path)
                current = None
                if node and not stat.S_ISDIR(node[1].st_mode):
                    obj,mode,_ = self._read(path,node,entry);current=(obj.oid(self.repo.object_format),mode)
                working = change((entry.oid,entry.mode),current)
            if staged+working != "  ":result.append(StatusEntry(path,staged,working))
        for path,node in nodes.items():
            if path not in tracked and not stat.S_ISDIR(node[1].st_mode) and not node[2]:result.append(StatusEntry(path,"?","?"))
        return tuple(sorted(result,key=lambda e:(e.index=="?",e.path)))
