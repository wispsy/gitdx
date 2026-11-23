"""A standalone Git engine with byte-preserving, format-aware Python APIs."""

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .objects import Blob, Commit, Header, ObjectFormat, ObjectId, ObjectType, RawObject, Tag, Tree, TreeEntry, parse_object
from .filesystem import UnsafePath
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded, ObjectStore, StoreLimits
from .repository import Repository, RepositoryNotFound
from .names import InvalidRef
from .refs import DirectRef, FilesRefStore, RefConflict, RefLocked, RefStore, ResolvedRef, SymbolicRef
from .index import Index, IndexEntry, IndexExtension, IndexFile, IndexLimits, IndexLocked, IndexStat, InvalidIndex
from .packed_refs import PackedRef, PackedRefs
from .reflog import ReflogReceipt, ReflogUpdate
from .refs import RefUpdateResult
from .pack import InvalidPack, PackFile, PackIndex, PackIndexEntry, PackLimits, RepositoryObjectStore
from .working_tree import IgnoredPath, StatusEntry, WorkingTree, WorktreeLimits
from .delta import apply_delta

__all__ = ["Blob", "Commit", "GitdxError", "Header", "InvalidObject", "ObjectFormat",
           "ObjectId", "ObjectType", "RawObject", "Tag", "Tree", "TreeEntry",
           "UnsupportedFormat", "parse_object", "UnsafePath", "LooseObjectStore", "MissingObject",
           "ObjectLimitExceeded", "ObjectStore", "StoreLimits", "Repository", "RepositoryNotFound", "InvalidRef",
           "DirectRef", "FilesRefStore", "RefConflict", "RefLocked", "RefStore", "ResolvedRef", "SymbolicRef"]
__all__ += ["Index", "IndexEntry", "IndexExtension", "IndexFile", "IndexLimits", "IndexLocked", "IndexStat", "InvalidIndex"]
__all__ += ["PackedRef", "PackedRefs", "ReflogReceipt", "ReflogUpdate", "RefUpdateResult"]
__all__ += ["InvalidPack", "PackFile", "PackIndex", "PackIndexEntry", "PackLimits", "RepositoryObjectStore"]
__all__ += ["IgnoredPath", "StatusEntry", "WorkingTree", "WorktreeLimits"]
__all__ += ["apply_delta"]


def main() -> None:
    from .cli import main as run
    raise SystemExit(run())
