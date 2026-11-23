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
from .history import HistoryEntry, HistoryLimits
from .pack_writer import PackBundle, PackWriteLimits, reachable_objects, write_pack
from .checkout import CheckoutChange, CheckoutConflict, CheckoutLimits, CheckoutPlan, CheckoutRecoveryRequired

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
__all__ += ["HistoryEntry", "HistoryLimits"]
__all__ += ["PackBundle", "PackWriteLimits", "reachable_objects", "write_pack"]
__all__ += ["CheckoutChange", "CheckoutConflict", "CheckoutLimits", "CheckoutPlan", "CheckoutRecoveryRequired"]


def main() -> None:
    from .cli import main as run
    raise SystemExit(run())
