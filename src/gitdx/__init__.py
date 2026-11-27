"""A standalone Git engine with byte-preserving, format-aware Python APIs."""

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .objects import Blob, Commit, Header, ObjectFormat, ObjectId, ObjectType, RawObject, Tag, Tree, TreeEntry, parse_object
from .filesystem import UnsafePath
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded, ObjectStore, StoreLimits
from .repository import Repository, RepositoryNotFound
from .config import Config, ConfigEntry, RepositoryConfig, parse_config, parse_values, read_config
from .names import InvalidRef
from .refs import DirectRef, FilesRefStore, RefConflict, RefLocked, RefStore, ResolvedRef, SymbolicRef
from .reftable import InvalidReftable, Reftable, ReftableLimits, ReftableRefStore
from .index import Index, IndexEntry, IndexExtension, IndexFile, IndexLimits, IndexLocked, IndexStat, InvalidIndex
from .packed_refs import PackedRef, PackedRefs
from .reflog import ReflogReceipt, ReflogUpdate
from .refs import RefUpdateResult
from .ref_transaction import (RefChange, RefRecoveryRequired, RefTransactionInterrupted,
                              RefTransactionResult, RefTransactionState)
from .pack import InvalidPack, PackFile, PackIndex, PackIndexEntry, PackLimits, RepositoryObjectStore
from .working_tree import IgnoredPath, StatusEntry, WorkingTree, WorktreeLimits
from .delta import apply_delta
from .history import HistoryEntry, HistoryLimits
from .diff import BlobDiff, DiffHunk, DiffLimits, DiffLine, FileDiff, diff_bytes, diff_trees, quote_path, render_patch
from .revisions import AmbiguousRevision, RevisionLimits, RevisionNotFound, UnsupportedRevision, resolve_revision
from .ancestry import is_ancestor, merge_bases
from .merge import MergeConflict, MergeLimits, MergePlan, MergeResult
from .worktree_transaction import MutationPlan, MutationResult
from .reset_restore import ResetPlan, RestorePlan
from .pack_writer import PackBundle, PackWriteLimits, reachable_objects, write_pack
from .translation import GraphTranslator, TranslationResult
from .upload_pack import UploadPack, UploadPackLimits
from .checkout import CheckoutChange, CheckoutConflict, CheckoutLimits, CheckoutPlan, CheckoutRecoveryRequired
from .checkout_transaction import CheckoutResult
from .quarantine import ObjectQuarantine, unpack_objects
from .quarantine import QuarantineOwnershipError
from .ingestion import IngestionLimits, IngestionProfile
from .confined_fs import ConfinementError, FilesystemLimits
from .receive import (ReceiveInterrupted, ReceiveLimits, ReceiveOutcome, ReceivePolicy, ReceiveRejected,
                      ReceiveResult, ReceiveState, ReceiveView, receive, recover_receive)
from .transfers import (CloneIncomplete, FetchFailure, FetchInterrupted, FetchResult, FetchTransport, LocalTransport,
                        TransferCancelled, TransferControl, TransferError, TransferLimits, TransferTimeout, clone, fetch)
from .http_transport import HTTPAuthorization, SmartHTTPTransport
from .capabilities import CapabilityState, QualificationEvidence, capability_report, implementation_digest, runtime_toolchain
from .push_protocol import (CommandStatus, PushCommand, PushRequest, ReportOption, ReportStatus, encode_push_request,
                            encode_report_status, negotiate_push, parse_push_request, parse_report_status)
from .push import PushOutcome, PushResult, PushState, PushTransport, PushUncertain, PushUpdate, push
from .pktline import (DELIMITER, FLUSH, RESPONSE_END, Packet, PacketDecoder, PacketKind, ProtocolBudget,
                      ProtocolCancelled, ProtocolError, ProtocolLimitExceeded, ProtocolLimits, ProtocolTimeout,
                      RemoteProtocolError, Sideband, decode_sideband, encode_packet, encode_sideband, read_packets, write_packets)
from .protocol import (Advertisement, AdvertisedRef, Capability, LegacyRequest, Negotiated, ObjectFormatMismatch, V2Command,
                       encode_advertisement, encode_legacy_request, encode_v2_command, negotiate, parse_ack,
                       parse_advertisement, parse_legacy_request, parse_ls_refs, parse_v2_command)

__all__ = ["Blob", "Commit", "GitdxError", "Header", "InvalidObject", "ObjectFormat",
           "ObjectId", "ObjectType", "RawObject", "Tag", "Tree", "TreeEntry",
           "UnsupportedFormat", "parse_object", "UnsafePath", "LooseObjectStore", "MissingObject",
           "ObjectLimitExceeded", "ObjectStore", "StoreLimits", "Repository", "RepositoryNotFound", "InvalidRef",
           "DirectRef", "FilesRefStore", "RefConflict", "RefLocked", "RefStore", "ResolvedRef", "SymbolicRef"]
__all__ += ["Index", "IndexEntry", "IndexExtension", "IndexFile", "IndexLimits", "IndexLocked", "IndexStat", "InvalidIndex"]
__all__ += ['InvalidReftable','Reftable','ReftableLimits','ReftableRefStore']
__all__ += ['UploadPack','UploadPackLimits']
__all__ += ["PackedRef", "PackedRefs", "ReflogReceipt", "ReflogUpdate", "RefUpdateResult"]
__all__ += ["RefChange", "RefRecoveryRequired", "RefTransactionInterrupted", "RefTransactionResult", "RefTransactionState"]
__all__ += ["InvalidPack", "PackFile", "PackIndex", "PackIndexEntry", "PackLimits", "RepositoryObjectStore"]
__all__ += ["IgnoredPath", "StatusEntry", "WorkingTree", "WorktreeLimits"]
__all__ += ["apply_delta"]
__all__ += ["HistoryEntry", "HistoryLimits"]
__all__ += ["BlobDiff", "DiffHunk", "DiffLimits", "DiffLine", "FileDiff", "diff_bytes", "diff_trees", "quote_path", "render_patch"]
__all__ += ["AmbiguousRevision", "RevisionLimits", "RevisionNotFound", "UnsupportedRevision", "resolve_revision"]
__all__ += ["is_ancestor", "merge_bases"]
__all__ += ["MergeConflict", "MergeLimits", "MergePlan", "MergeResult"]
__all__ += ["MutationPlan", "MutationResult"]
__all__ += ["ResetPlan", "RestorePlan"]
__all__ += ["PackBundle", "PackWriteLimits", "reachable_objects", "write_pack"]
__all__ += ["GraphTranslator", "TranslationResult"]
__all__ += ["CheckoutChange", "CheckoutConflict", "CheckoutLimits", "CheckoutPlan", "CheckoutRecoveryRequired"]
__all__ += ["CheckoutResult"]
__all__ += ["Config", "ConfigEntry", "RepositoryConfig", "parse_config", "parse_values", "read_config"]
__all__ += ["ObjectQuarantine", "unpack_objects"]
__all__ += ["IngestionLimits", "IngestionProfile"]
__all__ += ["ConfinementError", "FilesystemLimits"]
__all__ += ["QuarantineOwnershipError", "ReceiveInterrupted", "ReceiveLimits", "ReceiveOutcome", "ReceivePolicy",
            "ReceiveRejected", "ReceiveResult", "ReceiveState", "ReceiveView", "receive"]
__all__ += ["recover_receive"]
__all__ += ["HTTPAuthorization", "SmartHTTPTransport"]
__all__ += ["CapabilityState", "QualificationEvidence", "capability_report", "implementation_digest", "runtime_toolchain"]
__all__ += ["PushOutcome", "PushResult", "PushState", "PushTransport", "PushUncertain", "PushUpdate", "push"]
__all__ += ["CommandStatus", "PushCommand", "PushRequest", "ReportOption", "ReportStatus", "encode_push_request",
            "encode_report_status", "negotiate_push", "parse_push_request", "parse_report_status"]
__all__ += ["CloneIncomplete", "FetchFailure", "FetchInterrupted", "FetchResult", "FetchTransport", "LocalTransport",
            "TransferCancelled", "TransferControl", "TransferError", "TransferLimits", "TransferTimeout", "clone", "fetch"]
__all__ += ["DELIMITER", "FLUSH", "RESPONSE_END", "Packet", "PacketDecoder", "PacketKind", "ProtocolBudget", "ProtocolCancelled",
            "ProtocolError", "ProtocolLimitExceeded", "ProtocolLimits", "ProtocolTimeout", "RemoteProtocolError", "Sideband",
            "decode_sideband", "encode_packet", "encode_sideband", "read_packets", "write_packets"]
__all__ += ["Advertisement", "AdvertisedRef", "Capability", "LegacyRequest", "Negotiated", "ObjectFormatMismatch", "V2Command",
            "encode_advertisement", "encode_legacy_request", "encode_v2_command", "negotiate", "parse_ack", "parse_advertisement",
            "parse_legacy_request", "parse_ls_refs", "parse_v2_command"]


def main() -> None:
    from .cli import main as run
    raise SystemExit(run())
