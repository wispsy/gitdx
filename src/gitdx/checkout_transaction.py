"""Explicit checkout application and guarded journal rollback/finalization."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .checkout import (CheckoutConflict, CheckoutLimits, CheckoutPlan, CheckoutRecoveryRequired,
                       FileValue, _Budget, _capture, _index_bytes, _parents, _profile, plan_checkout)
from .errors import GitdxError
from .filesystem import check_path, read_file, replace_file
from .index import Index
from .objects import ObjectId
from .refs import DirectRef, SymbolicRef
from .working_tree import WorkingTree, WorktreeLimits, portable_path


MAX_RECEIPT = 32*1024*1024
MAX_JOURNAL_BYTES = 512*1024*1024


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    target: ObjectId | None
    head: DirectRef | SymbolicRef
    rolled_back: bool = False
    cleanup_pending: bool = False


def _ref(value):
    if value is None:return None
    return {"symbolic":value.target} if isinstance(value,SymbolicRef) else {"oid":str(value.oid)}


def _parse_ref(value, format):
    if value is None:return None
    if type(value) is not dict:raise ValueError("invalid ref record")
    if set(value)=={"symbolic"}:return SymbolicRef(value["symbolic"])
    if set(value)=={"oid"}:return DirectRef(ObjectId(value["oid"],format))
    raise ValueError("invalid ref record")


def _head_bytes(value):
    return (b"ref: "+value.target.encode("ascii") if isinstance(value,SymbolicRef) else str(value.oid).encode("ascii"))+b"\n"


@contextmanager
def _locks(repo, source_name, target_name, *, index=False):
    refs = repo.refs
    with ExitStack() as stack:
        if index:stack.enter_context(refs._lock(repo.git_dir/"index"))
        stack.enter_context(refs._lock(repo.git_dir/"packed-refs"))
        stack.enter_context(refs._lock(repo.git_dir/"HEAD"))
        for name in sorted({source_name,target_name}-{"HEAD"}):stack.enter_context(refs._lock(refs._path(name)))
        refs._profile()
        yield


def _guard_refs(repo, head, source_name, source_value, target_name, target_value):
    refs = repo.refs
    if refs.read("HEAD")!=head:raise CheckoutConflict("HEAD changed since checkout planning")
    if source_name!="HEAD" and refs.read(source_name)!=source_value:raise CheckoutConflict("source branch changed since checkout planning")
    if target_name!="HEAD" and refs.read(target_name)!=target_value:raise CheckoutConflict("target branch changed since checkout planning")


def _write_payload(root, name, data):
    path = root/name
    with path.open("xb") as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    return {"file":name,"size":len(data),"sha256":hashlib.sha256(data).hexdigest()}


def _save(root, receipt):
    data = json.dumps(receipt,sort_keys=True,separators=(",",":")).encode("ascii")
    if len(data)>MAX_RECEIPT:raise CheckoutConflict("checkout journal metadata limit exceeded")
    replace_file(root,root/"receipt.json",data)


def _prepare(repo, plan, new_head):
    temporary = Path(tempfile.mkdtemp(prefix="tmp_checkout_",dir=repo.git_dir))
    try:
        receipt = {"version":1,"format":repo.object_format.value,"token":uuid.uuid4().hex,"phase":"prepared",
                   "head_before":_ref(plan.head),"head_after":_ref(new_head),"source_name":plan.source_name,
                   "source_value":_ref(plan.source_value),"target_name":plan.target_name,"target_value":_ref(plan.target_value),
                   "config":hashlib.sha256(plan.config).hexdigest(),"limits":{n:getattr(plan.limits,n) for n in plan.limits.__slots__},"paths":[]}
        receipt["index_before"] = _write_payload(temporary,"index-before",plan.old_index) if plan.old_index is not None else None
        receipt["index_after"] = _write_payload(temporary,"index-after",plan.new_index)
        before,after = dict(plan.before),dict(plan.after);total = len(plan.old_index or b"")+len(plan.new_index)
        for i,change in enumerate(plan.changes):
            record = {"path":change.path.hex()}
            for side,label,values in (("before","a",before),("after","b",after)):
                value = values[change.path]
                record[side] = None
                if value is not None:
                    total += len(value.data)
                    if total>MAX_JOURNAL_BYTES:raise CheckoutConflict("checkout journal byte limit exceeded")
                    record[side] = {"mode":value.mode,"permissions":value.permissions,"data":_write_payload(temporary,f"p{i}-{label}",value.data)}
            receipt["paths"].append(record)
        _save(temporary,receipt)
        state = repo.git_dir/"checkout-state"
        if state.exists():raise CheckoutRecoveryRequired("checkout recovery is pending")
        os.rename(temporary,state)
        return receipt
    finally:
        if temporary.exists():shutil.rmtree(temporary)


def _atomic_leaf(repo, raw, value):
    path = repo.work_tree/os.fsdecode(raw);check_path(repo.work_tree,path.parent)
    fd,name = tempfile.mkstemp(prefix=".gitdx-checkout-",dir=path.parent);temporary=Path(name)
    try:
        if value.mode==0o120000:
            os.close(fd);temporary.unlink();os.symlink(os.fsdecode(value.data),temporary)
        else:
            with os.fdopen(fd,"wb") as stream:
                stream.write(value.data);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,value.permissions)
        # Do not follow a leaf symlink or overwrite an unexpected late arrival.
        if path.exists() or path.is_symlink():raise CheckoutConflict("working path changed during checkout")
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def _validate_states(repo, before, after, limits, *, exact=None):
    budget = _Budget(limits);current = {}
    for path in before:
        value = _capture(repo.work_tree,path,budget);current[path]=value
        allowed = (exact[path],) if exact is not None else (before[path],after[path],None)
        if exact is None and any(v and v.mode==0o40000 for v in (before[path],after[path])):
            allowed += (FileValue(0o40000,b"",0o777 if os.name=="nt" else 0o700),)
        if value not in allowed:raise CheckoutConflict("working content changed outside the pending checkout")
    return current


def _install(repo, before, after, desired, limits):
    current = _validate_states(repo,before,after,limits)
    removed_dirs = {p for p,v in current.items() if v and v.mode==0o40000 and (desired[p] is None or desired[p].mode!=0o40000)}
    if removed_dirs:
        work=WorkingTree(repo,limits=WorktreeLimits(limits.max_paths,limits.max_file_bytes,limits.max_bytes))
        nodes=work._paths(repo.index.read(),force=True)
        for path in nodes:
            if path not in desired and any(p in removed_dirs for p in _parents(path)):
                raise CheckoutConflict("untracked content prevents directory replacement or recovery")
    for path in sorted(current,key=lambda p:(p.count(b"/"),p),reverse=True):
        value=current[path]
        if value and value.mode!=0o40000 and value!=desired[path]:
            check_path(repo.work_tree,(repo.work_tree/os.fsdecode(path)).parent)
            if _capture(repo.work_tree,path,_Budget(limits))!=value:raise CheckoutConflict("working content changed during checkout")
            (repo.work_tree/os.fsdecode(path)).unlink()
    for path in sorted(removed_dirs,key=lambda p:(p.count(b"/"),p),reverse=True):
        check_path(repo.work_tree,repo.work_tree/os.fsdecode(path))
        (repo.work_tree/os.fsdecode(path)).rmdir()
    for path,value in sorted(desired.items(),key=lambda item:(item[0].count(b"/"),item[0])):
        if value and value.mode==0o40000:
            target=repo.work_tree/os.fsdecode(path);check_path(repo.work_tree,target)
            if not target.exists():target.mkdir(mode=0o700)
            if os.name!="nt":os.chmod(target,value.permissions)
    for path,value in sorted(desired.items()):
        if value and value.mode!=0o40000 and _capture(repo.work_tree,path,_Budget(limits))!=value:
            _atomic_leaf(repo,path,value)
    _validate_states(repo,before,after,limits,exact=desired)


def _complete(repo, receipt):
    state=repo.git_dir/"checkout-state"
    allowed={"receipt.json",receipt["index_after"]["file"]}
    if receipt["index_before"] is not None:allowed.add(receipt["index_before"]["file"])
    for record in receipt["paths"]:
        for side in ("before","after"):
            if record[side] is not None:allowed.add(record[side]["data"]["file"])
    if {p.name for p in state.iterdir()}!=allowed:raise CheckoutConflict("unrecognized files in checkout journal")
    retired=repo.git_dir/("checkout-complete-"+receipt["token"])
    if retired.exists() or retired.is_symlink():raise CheckoutConflict("checkout cleanup destination already exists")
    os.rename(state,retired)
    try:shutil.rmtree(retired)
    except OSError:return True
    return False


def apply_checkout(repo, plan):
    if not isinstance(plan,CheckoutPlan) or plan.git_dir!=repo.git_dir:raise CheckoutConflict("checkout plan belongs to another repository")
    _profile(repo)
    with repo.index.transaction() as transaction:
        with _locks(repo,plan.source_name,plan.target_name):
            _guard_refs(repo,plan.head,plan.source_name,plan.source_value,plan.target_name,plan.target_value)
            fresh=plan_checkout(repo,plan.destination,unborn=plan.unborn,overwrite_tracked=plan.overwrite_tracked,limits=plan.limits)
            if fresh!=plan:raise CheckoutConflict("checkout inputs changed; review a fresh plan")
            new_head=DirectRef(plan.target) if plan.target_name=="HEAD" else SymbolicRef(plan.target_name)
            receipt=_prepare(repo,plan,new_head)
            all_before,all_after=dict(plan.before),dict(plan.after)
            before={c.path:all_before[c.path] for c in plan.changes}
            after={p:all_after[p] for p in before}
            try:
                _install(repo,before,after,after,plan.limits)
                # Checkout intentionally replaces the complete index snapshot,
                # including advisory extensions, while retaining its owned lock.
                transaction._index=Index.from_bytes(plan.new_index,repo.object_format);transaction.commit()
                replace_file(repo.git_dir,repo.git_dir/"HEAD",_head_bytes(new_head))
                receipt["phase"]="committed";_save(repo.git_dir/"checkout-state",receipt)
                pending=_complete(repo,receipt)
                return CheckoutResult(plan.target,new_head,cleanup_pending=pending)
            except Exception as error:raise CheckoutRecoveryRequired("checkout interrupted; explicit recovery is required") from error


def _load(repo):
    state=repo.git_dir/"checkout-state";check_path(repo.git_dir,state)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError("duplicate receipt key")
            result[key]=value
        return result
    receipt=json.loads(read_file(state,state/"receipt.json",MAX_RECEIPT),object_pairs_hook=unique)
    if receipt["version"]!=1 or receipt["format"]!=repo.object_format.value or receipt["phase"] not in ("prepared","rolling-back","rolled-back","committed") or not re.fullmatch(r"[0-9a-f]{32}",receipt["token"]):raise ValueError("invalid checkout receipt")
    limits=CheckoutLimits(**receipt["limits"]);before={};after={};total=0;files={"receipt.json"};names={}
    def payload(record):
        nonlocal total
        name=record["file"];size=record["size"]
        if not re.fullmatch(r"(?:index-(?:before|after)|p[0-9]{1,10}-[ab])",name) or name in files or type(size) is not int or not 0<=size<=MAX_JOURNAL_BYTES:raise ValueError("invalid checkout payload")
        total+=size
        if total>MAX_JOURNAL_BYTES:raise ValueError("oversized checkout journal")
        files.add(name);data=read_file(state,state/name,size)
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=record["sha256"]:raise ValueError("checkout payload mismatch")
        return data
    for record in receipt["paths"]:
        path=bytes.fromhex(record["path"])
        if not portable_path(path) or path in before or len(before)>=limits.max_paths:raise ValueError("invalid checkout path")
        from .checkout import _key
        key=_key(path)
        if key in names and names[key]!=path:raise ValueError("checkout journal has path aliases")
        names[key]=path
        for side,values in (("before",before),("after",after)):
            value=record[side];values[path]=None
            if value is not None:
                mode,permissions=value["mode"],value["permissions"]
                if mode not in (0o100644,0o100755,0o120000,0o40000) or type(permissions) is not int or not 0<=permissions<=0o7777:raise ValueError("invalid checkout file mode")
                data=payload(value["data"])
                if len(data)>limits.max_file_bytes or (mode==0o40000 and data) or (mode==0o120000 and (not data or b"\0" in data or len(data)>1023)):raise ValueError("invalid checkout value")
                values[path]=FileValue(mode,data,permissions)
    old_index=payload(receipt["index_before"]) if receipt["index_before"] is not None else None
    new_index=payload(receipt["index_after"])
    for data in (old_index,new_index):
        if data is not None:Index.from_bytes(data,repo.object_format)
    for name in ("source_name","target_name"):
        if receipt[name]!="HEAD" and not receipt[name].startswith("refs/heads/"):raise ValueError("invalid checkout branch")
        repo.refs._path(receipt[name])
    refs={name:_parse_ref(receipt[name],repo.object_format) for name in ("head_before","head_after","source_value","target_value")}
    if any(value is not None and not isinstance(value,DirectRef) for key,value in refs.items() if key.endswith("value")):raise ValueError("invalid checkout branch value")
    for name in ("head_before","head_after"):
        value=refs[name]
        if not isinstance(value,(DirectRef,SymbolicRef)):raise ValueError("missing checkout HEAD")
        if isinstance(value,SymbolicRef) and not value.target.startswith("refs/heads/"):raise ValueError("invalid checkout HEAD target")
        if isinstance(value,DirectRef):repo.refs._target("HEAD",value.oid)
    if (refs["head_before"].target if isinstance(refs["head_before"],SymbolicRef) else "HEAD")!=receipt["source_name"]:raise ValueError("inconsistent source HEAD")
    if (refs["head_after"].target if isinstance(refs["head_after"],SymbolicRef) else "HEAD")!=receipt["target_name"]:raise ValueError("inconsistent target HEAD")
    if {p.name for p in state.iterdir()}!=files:raise ValueError("unrecognized files in checkout journal")
    return receipt,refs,before,after,old_index,new_index,limits


def recover_checkout(repo):
    if repo.bare:raise CheckoutRecoveryRequired("checkout recovery requires a working tree")
    state=repo.git_dir/"checkout-state"
    if not state.exists():raise CheckoutRecoveryRequired("there is no pending checkout journal")
    try:receipt,refs,before,after,old_index,new_index,limits=_load(repo)
    except (OSError,ValueError,KeyError,TypeError,GitdxError) as error:
        raise CheckoutRecoveryRequired("checkout journal is invalid; inspect it before recovery") from error
    with _locks(repo,receipt["source_name"],receipt["target_name"],index=True):
        try:current_receipt=json.loads(read_file(state,state/"receipt.json",MAX_RECEIPT))
        except (OSError,ValueError) as error:raise CheckoutConflict("checkout journal changed before recovery acquired its locks") from error
        if current_receipt!=receipt:raise CheckoutConflict("checkout journal changed before recovery acquired its locks")
        current_head=repo.refs.read("HEAD")
        if current_head not in (refs["head_before"],refs["head_after"]):raise CheckoutConflict("HEAD changed outside the pending checkout")
        _guard_refs(repo,current_head,receipt["source_name"],refs["source_value"],receipt["target_name"],refs["target_value"])
        if _index_bytes(repo) not in (old_index,new_index):raise CheckoutConflict("index changed outside the pending checkout")
        config=read_file(repo.git_dir,repo.git_dir/"config",1024*1024)
        if hashlib.sha256(config).hexdigest()!=receipt["config"]:raise CheckoutConflict("config changed outside the pending checkout")
        committed=receipt["phase"]=="committed";finished=committed or receipt["phase"]=="rolled-back"
        desired=after if committed else before;head=refs["head_after"] if committed else refs["head_before"]
        if finished:
            _validate_states(repo,before,after,limits,exact=desired)
            if repo.refs.read("HEAD")!=head or _index_bytes(repo)!=(new_index if committed else old_index):raise CheckoutConflict("completed checkout state changed")
        else:
            _validate_states(repo,before,after,limits)
            receipt["phase"]="rolling-back";_save(state,receipt)
            _install(repo,before,after,before,limits)
            if old_index is None:(repo.git_dir/"index").unlink(missing_ok=True)
            else:replace_file(repo.git_dir,repo.git_dir/"index",old_index)
            replace_file(repo.git_dir,repo.git_dir/"HEAD",_head_bytes(head))
            receipt["phase"]="rolled-back";_save(state,receipt)
        pending=_complete(repo,receipt)
        target=head.oid if isinstance(head,DirectRef) else repo.refs.resolve().oid
        return CheckoutResult(target,head,rolled_back=not committed,cleanup_pending=pending)
