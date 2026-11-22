"""A standalone Git engine with byte-preserving, format-aware Python APIs."""

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .objects import Blob, Commit, Header, ObjectFormat, ObjectId, ObjectType, RawObject, Tag, Tree, TreeEntry, parse_object
from .filesystem import UnsafePath
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded, ObjectStore, StoreLimits

__all__ = ["Blob", "Commit", "GitdxError", "Header", "InvalidObject", "ObjectFormat",
           "ObjectId", "ObjectType", "RawObject", "Tag", "Tree", "TreeEntry",
           "UnsupportedFormat", "parse_object", "UnsafePath", "LooseObjectStore", "MissingObject",
           "ObjectLimitExceeded", "ObjectStore", "StoreLimits"]


def main() -> None:
    from .cli import main as run
    raise SystemExit(run())
