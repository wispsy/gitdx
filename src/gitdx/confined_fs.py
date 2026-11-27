"""Descriptor-relative storage operations for an explicitly selected Linux host.

The process owns the repository and its ancestors. Directory descriptors anchor
every operation; untrusted names never choose an absolute filesystem target.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
import uuid

from .errors import GitdxError, UnsupportedFormat


class ConfinementError(GitdxError, ValueError):
    """Storage identity, ownership or confinement cannot be established."""


@dataclass(frozen=True, slots=True)
class FilesystemLimits:
    max_entries: int = 200_000
    max_io_bytes: int = 1024 * 1024 * 1024
    max_depth: int = 64
    max_reserved_bytes: int = 768 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v<=0 for v in (self.max_entries,self.max_io_bytes,self.max_depth,self.max_reserved_bytes)):
            raise ValueError('filesystem limits must be positive integers')


_ACTIVE=ContextVar('gitdx_confined_storage',default=None)


def active():return _ACTIVE.get()


def supported():
    return (sys.platform=='linux' and hasattr(os,'O_NOFOLLOW') and hasattr(os,'O_DIRECTORY')
            and {os.open,os.mkdir,os.rename,os.unlink,os.rmdir,os.link,os.stat}<=os.supports_dir_fd)


class ConfinedStorage:
    def __init__(self,root,identity,*,limits=None,check=None,limit_error=ConfinementError):
        if not supported():raise UnsupportedFormat('untrusted storage requires the qualified Linux descriptor profile')
        self.root=Path(root).absolute();self.identity=identity
        self.limits=limits or FilesystemLimits();self.check=check or (lambda:None);self.limit_error=limit_error
        self.fd=None;self.entries=0;self.io_bytes=0;self.reserved=0;self._cleaning=None

    def _owned(self,info,*,directory=False):
        if (info.st_uid!=os.geteuid() or info.st_mode&0o022
                or directory and not stat.S_ISDIR(info.st_mode)
                or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1)):
            raise ConfinementError('hosting storage requires owner-controlled regular files and directories')
        if self.fd is not None and info.st_dev!=os.fstat(self.fd).st_dev:
            raise ConfinementError('hosting storage cannot cross filesystem boundaries')

    def __enter__(self):
        if active() is not None or self.fd is not None:raise ConfinementError('storage confinement cannot be nested or reused')
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
        self.fd=os.open(self.root,flags)
        try:
            info=os.fstat(self.fd);self._owned(info,directory=True)
            if not 0<os.fstatvfs(self.fd).f_frsize<=4096:
                raise UnsupportedFormat('hosting allocation profile requires filesystem fragments of at most 4096 bytes')
            if self.identity is None or (info.st_dev,info.st_ino)!=tuple(self.identity):
                raise ConfinementError('repository identity changed since opening')
            self.token=_ACTIVE.set(self)
        except BaseException:
            os.close(self.fd);self.fd=None;raise
        return self

    def __exit__(self,*exc):
        _ACTIVE.reset(self.token);os.close(self.fd);self.fd=None

    def unchanged(self):
        self.tick()
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            info=os.fstat(fd)
            if (info.st_dev,info.st_ino)!=tuple(self.identity):raise ConfinementError('repository root changed during ingestion')
        finally:os.close(fd)

    def parts(self,path):
        try:parts=Path(path).absolute().relative_to(self.root).parts
        except ValueError as error:raise ConfinementError('storage path escapes its selected repository') from error
        if len(parts)>self.limits.max_depth or any(p in ('.','..') or not p or '\0' in p for p in parts):
            raise ConfinementError('invalid or excessive storage path')
        return parts

    def charge(self,*,entries=0,size=0):
        if self._cleaning is not None:
            self._cleaning[0]+=entries;self._cleaning[1]+=size
            if self._cleaning[0]>2*self.limits.max_entries+1024 or self._cleaning[1]>1024*1024:
                raise self.limit_error('owned cleanup budget exceeded')
            return
        self.check();self.entries+=entries;self.io_bytes+=size
        if self.entries>self.limits.max_entries or self.io_bytes>self.limits.max_io_bytes:
            raise self.limit_error('hosting filesystem work/byte budget exceeded')

    def tick(self):
        if self._cleaning is None:self.check()

    @contextmanager
    def cleanup(self):
        previous=self._cleaning
        if previous is None:self._cleaning=[0,0]
        try:yield
        finally:self._cleaning=previous

    def reserve(self,size):
        self.check()
        if self.reserved+size>self.limits.max_reserved_bytes:
            raise self.limit_error('hosting filesystem allocation reservation exceeded')
        self.reserved+=size

    @contextmanager
    def directory(self,path,*,create=False):
        parts=self.parts(path);fd=os.dup(self.fd)
        try:
            for part in parts:
                self.charge(entries=1)
                if create:
                    try:os.stat(part,dir_fd=fd,follow_symlinks=False)
                    except FileNotFoundError:
                        self.reserve(4096)
                        try:os.mkdir(part,0o700,dir_fd=fd)
                        except FileExistsError:pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                try:self._owned(os.fstat(child),directory=True)
                except BaseException:os.close(child);raise
                os.close(fd);fd=child
            yield fd
        finally:os.close(fd)

    @contextmanager
    def parent(self,path,*,create=False):
        parts=self.parts(path)
        if not parts:raise ConfinementError('the repository root cannot be mutated as a leaf')
        with self.directory(Path(path).parent,create=create) as fd:yield fd,parts[-1]

    def info(self,path):
        if not self.parts(path):return os.fstat(self.fd)
        with self.parent(path) as (fd,name):return os.stat(name,dir_fd=fd,follow_symlinks=False)

    def exists(self,path):
        try:info=self.info(path)
        except FileNotFoundError:return False
        if stat.S_ISLNK(info.st_mode):raise ConfinementError('storage symlinks are refused')
        return True

    def read(self,path,limit,limit_error):
        with self.parent(path) as (parent,name):
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
            try:
                info=os.fstat(fd);self._owned(info)
                if info.st_size>limit:raise limit_error('storage file byte limit exceeded')
                result=bytearray()
                while True:
                    self.tick();chunk=os.read(fd,min(65536,limit+1-len(result)))
                    if not chunk:break
                    self.charge(size=len(chunk));result.extend(chunk)
                    if len(result)>limit:raise limit_error('storage file byte limit exceeded')
                return bytes(result)
            finally:os.close(fd)

    def write(self,path,data,*,exclusive=False,immutable=False,reserve=None):
        if type(data) is not bytes:raise TypeError('storage writes require immutable bytes')
        self.charge(size=len(data))
        self.reserve(((len(data)+4095)//4096)*4096)
        if reserve is not None:reserve(len(data))
        with self.parent(path,create=True) as (parent,name):
            temporary='tmp_secure_'+uuid.uuid4().hex
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                self.check()
                if exclusive or immutable:os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
                else:
                    try:info=os.stat(name,dir_fd=parent,follow_symlinks=False)
                    except FileNotFoundError:pass
                    else:self._owned(info)
                    os.replace(temporary,name,src_dir_fd=parent,dst_dir_fd=parent)
            finally:
                try:os.unlink(temporary,dir_fd=parent)
                except FileNotFoundError:pass

    def mkdir(self,path):
        with self.parent(path,create=True) as (fd,name):
            self.reserve(4096);os.mkdir(name,0o700,dir_fd=fd)

    def mkdtemp(self,path,prefix):
        for _ in range(8):
            candidate=Path(path)/(prefix+uuid.uuid4().hex)
            try:self.mkdir(candidate)
            except FileExistsError:continue
            return candidate
        raise ConfinementError('could not reserve private staging storage')

    def unlink(self,path,*,missing_ok=False):
        with self.parent(path) as (fd,name):
            try:
                info=os.stat(name,dir_fd=fd,follow_symlinks=False);self._owned(info)
                os.unlink(name,dir_fd=fd)
            except FileNotFoundError:
                if not missing_ok:raise

    def rename(self,source,target):
        with self.parent(source) as (src,a),self.parent(target) as (dst,b):
            info=os.stat(a,dir_fd=src,follow_symlinks=False);self._owned(info,directory=True)
            try:os.stat(b,dir_fd=dst,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise FileExistsError('storage destination already exists')
            os.rename(a,b,src_dir_fd=src,dst_dir_fd=dst)

    def names(self,path):
        with self.directory(path) as fd:
            with os.scandir(fd) as entries:
                result=[]
                for entry in entries:self.charge(entries=1);result.append(entry.name)
                return result

    def remove_tree(self,path,*,identity=None):
        # Reopen every child without following links. Bound every scan/depth;
        # malformed or replaced storage is retained for explicit reconciliation.
        def remove(fd,depth):
            if depth>self.limits.max_depth:raise self.limit_error('cleanup directory depth exceeded')
            with os.scandir(fd) as entries:
                names=[]
                for entry in entries:self.charge(entries=1);names.append(entry.name)
            for name in names:
                info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                    try:self._owned(os.fstat(child),directory=True);remove(child,depth+1)
                    finally:os.close(child)
                    current=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise ConfinementError('cleanup child was replaced')
                    os.rmdir(name,dir_fd=fd)
                else:
                    self._owned(info);os.unlink(name,dir_fd=fd)
        with self.parent(path) as (parent,name):
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            try:
                info=os.fstat(fd);self._owned(info,directory=True)
                if identity is not None and (info.st_dev,info.st_ino)!=tuple(identity):raise ConfinementError('cleanup ownership identity changed')
                remove(fd,0)
            finally:os.close(fd)
            current=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise ConfinementError('cleanup root was replaced')
            os.rmdir(name,dir_fd=parent)

    @contextmanager
    def lock(self,path):
        with self.parent(path,create=True) as (fd,name):
            lock=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=fd)
            info=os.fstat(lock);os.close(lock)
            try:yield
            finally:
                actual=os.stat(name,dir_fd=fd,follow_symlinks=False)
                if (actual.st_dev,actual.st_ino)!=(info.st_dev,info.st_ino):raise ConfinementError('owned lock was replaced')
                os.unlink(name,dir_fd=fd)
