"""Recoverable worktree/index/ref mutations sharing checkout's path journal."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os

from .checkout import (CheckoutConflict, CheckoutPlan, CheckoutRecoveryRequired,
                       _index_bytes)
from .checkout_transaction import (_complete, _guard_refs, _install,
                                   _load, _locks, _prepare, _save, _validate_states)
from .errors import GitdxError
from .filesystem import check_path, read_file, replace_file
from .objects import ObjectId
from .refs import DirectRef, SymbolicRef

STATE = "operation-state"
ALLOWED = frozenset(("merge-begin", "merge-finish", "merge-abort", "reset", "restore"))
MAX_OPERATION = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class MetadataChange:
    name: str
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True, slots=True)
class MutationPlan:
    checkout: CheckoutPlan
    operation: str
    metadata: tuple[MetadataChange, ...] = ()
    new_oid: ObjectId | None = None
    context: bytes = b"{}"
    remove_index: bool = False

    @property
    def changes(self):return self.checkout.changes


@dataclass(frozen=True, slots=True)
class MutationResult:
    operation: str
    head: ObjectId | None
    rolled_back: bool = False
    cleanup_pending: bool = False


def metadata(repo, name):
    check_path(repo.git_dir, repo.git_dir/name)
    try:return read_file(repo.git_dir, repo.git_dir/name, MAX_OPERATION)
    except FileNotFoundError:return None


def _packed(repo):
    data = metadata(repo, "packed-refs")
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _old_oid(plan):
    value = plan.source_value if isinstance(plan.head, SymbolicRef) else plan.head
    return value.oid if value is not None else None


def _validate_metadata(repo, source, changes):
    names = set()
    for change in changes:
        if not isinstance(change, MetadataChange) or change.name in names:
            raise ValueError("duplicate or invalid mutation metadata")
        if change.name not in {"HEAD", "ORIG_HEAD", "MERGE_HEAD", "MERGE_MSG", source}:
            raise ValueError("mutation metadata is outside its selected ref and operation files")
        if change.name.startswith("refs/"):repo.refs._path(change.name)
        for data in (change.before, change.after):
            if data is not None and (not isinstance(data, bytes) or len(data)>MAX_OPERATION):
                raise ValueError("mutation metadata must be bounded immutable bytes")
        names.add(change.name)


def make_mutation(repo, checkout, operation, *, metadata_changes=(), new_oid=None, context=None, remove_index=False):
    if operation not in ALLOWED:raise ValueError("unknown worktree mutation")
    if not isinstance(checkout, CheckoutPlan) or checkout.git_dir!=repo.git_dir:
        raise CheckoutConflict("mutation plan belongs to another repository")
    # The synthetic checkout is only a path/content planner. The operation
    # retains HEAD's symbolic identity; any tip change is an explicit CAS.
    checkout = replace(checkout, target_name=checkout.source_name,
                       target_value=checkout.source_value, target=_old_oid(checkout))
    changes = tuple(metadata_changes)
    if new_oid is not None:
        repo.refs._target(checkout.source_name, new_oid)
        changes += (MetadataChange(checkout.source_name, metadata(repo, checkout.source_name),
                                   new_oid.hex.encode("ascii")+b"\n"),)
    _validate_metadata(repo, checkout.source_name, changes)
    data = json.dumps(context or {}, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(data)>MAX_OPERATION:raise CheckoutConflict("mutation context limit exceeded")
    if type(remove_index) is not bool:raise TypeError("index removal must be an explicit boolean")
    return MutationPlan(checkout, operation, changes, new_oid, data, remove_index)


def _operation(repo, plan):
    return {"version":1, "name":plan.operation, "packed":_packed(repo), "remove_index":plan.remove_index,
            "new_oid":plan.new_oid.hex if plan.new_oid is not None else None,
            "git":[{"name":c.name, "before":c.before.hex() if c.before is not None else None,
                    "after":c.after.hex() if c.after is not None else None} for c in plan.metadata],
            "context":json.loads(plan.context)}


@contextmanager
def _locked(repo, source, changes):
    with _locks(repo, source, source, index=True), ExitStack() as stack:
        for change in sorted(changes, key=lambda c:c.name):
            if change.name not in (source, "HEAD"):
                stack.enter_context(repo.refs._lock(repo.git_dir/change.name))
        yield


def _put(repo, name, data):
    path = repo.git_dir/name;check_path(repo.git_dir, path)
    if data is None:path.unlink(missing_ok=True)
    else:replace_file(repo.git_dir, path, data)


def _context(repo, receipt, *, state_name=STATE):
    data = read_file(repo.git_dir/state_name, repo.git_dir/state_name/"operation", MAX_OPERATION)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:raise ValueError("duplicate operation field")
            result[key] = value
        return result
    record = json.loads(data, object_pairs_hook=unique)
    if set(record)!={"version", "name", "packed", "new_oid", "git", "context", "remove_index"} or record["version"]!=1 or record["name"] not in ALLOWED or type(record["remove_index"]) is not bool:
        raise ValueError("invalid operation manifest")
    if not isinstance(record["context"], dict) or not isinstance(record["git"], list) or len(record["git"])>5:
        raise ValueError("invalid operation metadata")
    changes = []
    for value in record["git"]:
        if set(value)!={"name", "before", "after"}:raise ValueError("invalid metadata fields")
        changes.append(MetadataChange(value["name"], *(bytes.fromhex(value[k]) if value[k] is not None else None for k in ("before", "after"))))
    _validate_metadata(repo, receipt["source_name"], changes)
    oid = ObjectId(record["new_oid"], repo.object_format) if record["new_oid"] is not None else None
    if oid is not None:
        repo.refs._target(receipt["source_name"], oid)
        selected = [c for c in changes if c.name==receipt["source_name"]]
        if len(selected)!=1 or selected[0].after!=oid.hex.encode()+b"\n":raise ValueError("invalid selected ref mutation")
    elif any(c.name in (receipt["source_name"], "HEAD") for c in changes):
        raise ValueError("unexpected ref mutation")
    return record, tuple(changes), oid


def load_mutation(repo, *, state_name=STATE):
    try:
        loaded = _load(repo, state_name=state_name)
        record, changes, oid = _context(repo, loaded[0], state_name=state_name)
        return loaded, record, changes, oid
    except (OSError, ValueError, KeyError, TypeError, GitdxError) as error:
        raise CheckoutRecoveryRequired("operation journal is invalid; inspect it before recovery") from error


def _guard(repo, receipt, refs, record, changes, oid, *, after=None):
    head = repo.refs.read("HEAD")
    expected = (refs["head_after"],) if after is True else (refs["head_before"],) if after is False else (refs["head_before"], refs["head_after"])
    if head not in expected:raise CheckoutConflict("HEAD changed outside the pending operation")
    old = refs["source_value"].oid if refs["source_value"] is not None else refs["head_before"].oid if isinstance(refs["head_before"], DirectRef) else None
    selected = oid if oid is not None else old
    expected_ids = (selected,) if after is True else (old,) if after is False else (old, selected)
    if repo.refs.resolve().oid not in expected_ids:raise CheckoutConflict("branch changed outside the pending operation")
    if _packed(repo)!=record["packed"]:raise CheckoutConflict("packed refs changed outside the pending operation")
    if hashlib.sha256(read_file(repo.git_dir, repo.git_dir/"config", 1024*1024)).hexdigest()!=receipt["config"]:
        raise CheckoutConflict("configuration changed outside the pending operation")
    for change in changes:
        allowed = (change.after,) if after is True else (change.before,) if after is False else (change.before, change.after)
        if metadata(repo, change.name) not in allowed:raise CheckoutConflict("operation metadata changed externally")


def _finalize(repo, receipt, record):
    if record["name"]=="merge-begin":
        destination = repo.git_dir/"merge-state";check_path(repo.git_dir, destination)
        if destination.exists():raise CheckoutConflict("merge state already exists")
        os.rename(repo.git_dir/STATE, destination)
        return False
    pending = False
    if record["name"] in ("merge-finish", "merge-abort"):
        if (repo.git_dir/"merge-state").exists():
            active, _, _, _ = load_mutation(repo, state_name="merge-state")
            if active[0]["token"]!=record["context"].get("merge_token"):
                raise CheckoutConflict("active merge changed during completion")
            pending = _complete(repo, active[0], state_name="merge-state")
    return _complete(repo, receipt, state_name=STATE) or pending


def apply_mutation(repo, plan, *, validate):
    if not isinstance(plan, MutationPlan) or plan.checkout.git_dir!=repo.git_dir:
        raise CheckoutConflict("mutation belongs to another repository")
    base = plan.checkout
    check_path(repo.git_dir, repo.git_dir/STATE)
    if (repo.git_dir/STATE).exists():raise CheckoutRecoveryRequired("operation recovery is pending")
    with _locked(repo, base.source_name, plan.metadata):
        _guard_refs(repo, base.head, base.source_name, base.source_value, base.source_name, base.source_value)
        validate()
        if _index_bytes(repo)!=base.old_index:raise CheckoutConflict("index changed since planning")
        before = {c.path:dict(base.before)[c.path] for c in base.changes}
        after = {p:dict(base.after)[p] for p in before}
        _validate_states(repo, before, after, base.limits, exact=before)
        for change in plan.metadata:
            if metadata(repo, change.name)!=change.before:raise CheckoutConflict("operation metadata changed since planning")
        record = _operation(repo, plan)
        data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(data)>MAX_OPERATION:raise CheckoutConflict("operation manifest limit exceeded")
        new_head = DirectRef(plan.new_oid) if plan.new_oid is not None and base.source_name=="HEAD" else base.head
        receipt = _prepare(repo, base, new_head, state_name=STATE, operation=data)
        try:
            _install(repo, before, after, after, base.limits)
            after_index = None if plan.remove_index else base.new_index
            if after_index!=base.old_index:_put(repo, "index", after_index)
            for change in plan.metadata:
                if change.before!=change.after:_put(repo, change.name, change.after)
            receipt["phase"] = "committed";_save(repo.git_dir/STATE, receipt)
            pending = _finalize(repo, receipt, record)
            return MutationResult(plan.operation, repo.refs.resolve().oid, cleanup_pending=pending)
        except Exception as error:
            raise CheckoutRecoveryRequired("operation interrupted; explicit recovery is required") from error


def recover_mutation(repo):
    loaded, record, changes, oid = load_mutation(repo)
    receipt, refs, before, after, old_index, new_index, limits = loaded
    after_index = None if record["remove_index"] else new_index
    with _locked(repo, receipt["source_name"], changes):
        current = load_mutation(repo)
        if current[0][0]!=receipt:raise CheckoutConflict("operation journal changed while locking")
        _guard(repo, receipt, refs, record, changes, oid)
        committed = receipt["phase"]=="committed"
        if committed:
            _guard(repo, receipt, refs, record, changes, oid, after=True)
            _validate_states(repo, before, after, limits, exact=after)
            if _index_bytes(repo)!=after_index:raise CheckoutConflict("committed operation index changed")
        else:
            if _index_bytes(repo) not in (old_index, after_index):raise CheckoutConflict("operation index changed externally")
            receipt["phase"] = "rolling-back";_save(repo.git_dir/STATE, receipt)
            _install(repo, before, after, before, limits)
            if _index_bytes(repo)!=old_index:_put(repo, "index", old_index)
            for change in reversed(changes):
                if metadata(repo,change.name)!=change.before:_put(repo, change.name, change.before)
            receipt["phase"] = "rolled-back";_save(repo.git_dir/STATE, receipt)
        pending = _finalize(repo, receipt, record) if committed else _complete(repo, receipt, state_name=STATE)
        return MutationResult(record["name"], repo.refs.resolve().oid, rolled_back=not committed, cleanup_pending=pending)
