"""Bounded two-parent merge plans, explicit resolution and guarded recovery."""

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
import json

from .checkout import (CheckoutChange, CheckoutConflict, CheckoutLimits, CheckoutPlan,
                       CheckoutRecoveryRequired, _Budget, _capture, _index_bytes,
                       _parents, _profile, _tree, _value_id, plan_checkout)
from .errors import InvalidObject, UnsupportedFormat
from .filesystem import read_file
from .history import HistoryLimits, read_commit, validate_identity, write_tree
from .index import Index, IndexEntry
from .object_store import ObjectLimitExceeded
from .objects import Blob, Commit, ObjectId, ObjectType
from .refs import DirectRef, RefConflict, SymbolicRef
from .worktree_transaction import (MetadataChange, MutationPlan, _guard, apply_mutation,
                                   load_mutation, make_mutation, metadata, recover_mutation)

PLAN_IDENTITY = b"Merge plan <plan@example.invalid> 0 +0000"


@dataclass(frozen=True, slots=True)
class MergeLimits:
    checkout: CheckoutLimits = CheckoutLimits()
    max_text_lines: int = 100_000
    max_text_work: int = 4_000_000

    def __post_init__(self):
        if not isinstance(self.checkout, CheckoutLimits):raise TypeError("select typed checkout limits")
        if any(type(getattr(self, name)) is not int or getattr(self, name)<=0 for name in ("max_text_lines", "max_text_work")):
            raise ValueError("merge limits must be positive integers")


@dataclass(frozen=True, slots=True)
class MergeConflict:
    path: bytes
    kind: str
    stages: tuple[IndexEntry, ...]


@dataclass(frozen=True, slots=True)
class MergePlan:
    revision: str | ObjectId
    head: ObjectId
    other: ObjectId
    base: ObjectId | None
    limits: MergeLimits
    index: Index
    conflicts: tuple[MergeConflict, ...]
    mutation: MutationPlan | None
    objects: tuple = field(default=(), repr=False)
    explicit_base: ObjectId | None = None

    @property
    def changes(self):return self.mutation.checkout.changes if self.mutation else ()


@dataclass(frozen=True, slots=True)
class MergeResult:
    state: str
    head: ObjectId
    other: ObjectId
    conflicts: tuple[MergeConflict, ...] = ()


class _Overlay:
    def __init__(self, store):self.base=store;self.format=store.format;self.added={}
    def read(self, oid):return self.added[oid] if oid in self.added else self.base.read(oid)
    def write(self, raw):
        oid=raw.oid(self.format);self.added[oid]=raw;return oid


def _snapshot(repo, overlay, wanted, limits):
    virtual=replace(repo, objects=overlay)
    index=Index(tuple(IndexEntry(path, oid, mode) for path,(oid,mode) in wanted.items()), repo.object_format)
    tree=write_tree(virtual, index, HistoryLimits(max_paths=limits.max_paths, max_depth=limits.max_depth, max_bytes=limits.max_bytes))
    target=overlay.write(Commit.create(tree, author=PLAN_IDENTITY, committer=PLAN_IDENTITY, message=b"Private merge plan\n").to_raw())
    return plan_checkout(virtual, target, limits=limits)


def _lines(data):
    parts=data.split(b"\n")
    return [p+b"\n" for p in parts[:-1]]+([parts[-1]] if parts[-1] else [])


def _text(base, ours, theirs, limits, work):
    if any(data.count(b"\n")+bool(data and not data.endswith(b"\n"))>limits.max_text_lines for data in (base,ours,theirs)):
        raise ObjectLimitExceeded("merge line count exceeded")
    a,b,c=(_lines(data) for data in (base,ours,theirs))
    if max(map(len,(a,b,c)))>limits.max_text_lines:raise ObjectLimitExceeded("merge line count exceeded")
    work[0]+=len(a)*(len(b)+len(c))
    if work[0]>limits.max_text_work:raise ObjectLimitExceeded("merge text comparison budget exceeded")
    edits=[]
    for side,lines in enumerate((b,c)):
        for tag,start,end,left,right in SequenceMatcher(None,a,lines,autojunk=False).get_opcodes():
            if tag!="equal":edits.append((start,end,tuple(lines[left:right]),side))
    edits.sort(key=lambda e:(e[0],e[1],e[3]));selected=[]
    for edit in edits:
        duplicate=False
        for other in reversed(selected):
            if other[1]<edit[0]:break
            if edit[:3]==other[:3]:duplicate=True;break
            # Touching edits from opposite sides are conservatively conflicts;
            # this does not promise every native merge alignment heuristic.
            if edit[3]!=other[3] and max(edit[0],other[0])<=min(edit[1],other[1]):return None
        if not duplicate:selected.append(edit)
    result=[];cursor=0
    for start,end,value,_ in selected:result.extend(a[cursor:start]);result.extend(value);cursor=end
    result.extend(a[cursor:]);return b"".join(result)


def _stages(path, values):
    return tuple(IndexEntry(path, value[0], value[1], stage=stage)
                 for stage,value in enumerate(values,1) if value is not None)


def _markers(ours, theirs):
    def terminated(data):return data+(b"\n" if data and not data.endswith(b"\n") else b"")
    return b"<<<<<<< HEAD\n"+terminated(ours)+b"=======\n"+terminated(theirs)+b">>>>>>> MERGE_HEAD\n"


def plan_merge(repo, revision, *, base=None, limits=None):
    """Plan a two-parent/no-ff merge without writing objects or repository state."""
    limits=limits or MergeLimits();_profile(repo)
    head=repo.refs.resolve().oid
    if head is None:raise UnsupportedFormat("merge requires an existing HEAD commit")
    other=repo.resolve(revision);read_commit(repo,other)
    # A self checkout validates a clean staged/worktree baseline, including
    # configuration and path aliases, without discarding unrelated untracked files.
    clean=plan_checkout(repo,head,limits=limits.checkout)
    if other==head or repo.is_ancestor(other,head):
        return MergePlan(revision,head,other,None,limits,repo.index.read(),(),None,explicit_base=base)
    bases=repo.merge_bases(head,other)
    if not bases:raise UnsupportedFormat("unrelated-history merges require a separate policy")
    if base is None:
        if len(bases)!=1:raise UnsupportedFormat("select one of the multiple best merge bases explicitly")
        selected=bases[0]
    else:
        if not isinstance(base,ObjectId) or base not in bases:raise ValueError("explicit base must be a verified best common ancestor")
        selected=base
    budget=_Budget(limits.checkout)
    original,ours,theirs=(_tree(repo,oid,limits.checkout,budget) for oid in (selected,head,other))
    paths=set(original)|set(ours)|set(theirs)
    if len(paths)>limits.checkout.max_paths:raise ObjectLimitExceeded("merge path count exceeded")
    overlay=_Overlay(repo.objects);working={};entries={};conflicts={};work=[0]
    def blob(value):
        if value is None:return b""
        raw=repo.objects.read(value[0]);budget.add(len(raw.data))
        if raw.kind is not ObjectType.BLOB:raise InvalidObject("merge file must name a blob")
        return raw.data
    for path in sorted(paths):
        old,left,right=(tree.get(path) for tree in (original,ours,theirs))
        if left==right:result=left
        elif left==old:result=right
        elif right==old:result=left
        else:
            result=None;kind="content";merged=None
            if left is None or right is None:kind="modify-delete"
            elif old is None:kind="add-add"
            elif left[1]&0o170000!=right[1]&0o170000:kind="type"
            elif left[1] not in (0o100644,0o100755):kind="binary"
            else:
                mode=left[1] if left[1]==right[1] or right[1]==old[1] else right[1] if left[1]==old[1] else None
                a,b,c=(blob(value) for value in (old,left,right))
                if mode is None:kind="mode"
                elif any(b"\0" in data for data in (a,b,c)):kind="binary"
                else:merged=_text(a,b,c,limits,work)
                if merged is not None:result=(overlay.write(Blob(merged).to_raw()),mode)
                elif kind=="content":
                    if len(b)+len(c)+48>limits.checkout.max_file_bytes:raise ObjectLimitExceeded("merge conflict output exceeded")
                    data=_markers(b,c)
                    if len(data)>limits.checkout.max_file_bytes:raise ObjectLimitExceeded("merge conflict output exceeded")
                    working[path]=(overlay.write(Blob(data).to_raw()),left[1])
            if result is None:
                conflicts[path]=MergeConflict(path,kind,_stages(path,(old,left,right)))
                if path not in working and (left or right):working[path]=left or right
        if path not in conflicts and result is not None:
            entries[path]=IndexEntry(path,result[0],result[1]);working[path]=result
    # Collapse directory/file conflicts into independent prefix groups. Keeping
    # ours in the worktree preserves its topology; all three index views survive.
    roots={parent for path in paths for parent in _parents(path) if parent in paths}
    top={root for root in roots if not (_parents(root)&roots)}
    groups={root:[] for root in top}
    for path in paths:
        for prefix in (path,*_parents(path)):
            if prefix in top:groups[prefix].append(path);break
    for group in groups.values():
        for path in group:
            entries.pop(path,None);working.pop(path,None)
            conflicts[path]=MergeConflict(path,"directory-file",_stages(path,(original.get(path),ours.get(path),theirs.get(path))))
        selected_tree=ours if any(p in ours for p in group) else theirs
        working.update((p,selected_tree[p]) for p in group if p in selected_tree)
    index=Index(tuple(entries.values())+tuple(e for c in conflicts.values() for e in c.stages),repo.object_format)
    checkout=_snapshot(repo,overlay,working,limits.checkout)
    before,after=dict(checkout.before),dict(checkout.after)
    guarded=set(conflicts)
    for path in tuple(guarded):guarded.update(_parents(path))
    for path in guarded:
        if path not in before:before[path]=_capture(repo.work_tree,path,budget);after[path]=before[path]
    if len(before)>limits.checkout.max_paths:raise ObjectLimitExceeded("merge expanded path limit exceeded")
    changed={c.path:c for c in checkout.changes}
    for path in guarded:
        changed.setdefault(path,CheckoutChange(path,before[path].mode if before[path] else None,after[path].mode if after[path] else None,
                                              _value_id(before[path],repo.object_format),_value_id(after[path],repo.object_format)))
    checkout=replace(checkout,new_index=index.encode(repo.index.limits),changes=tuple(changed[p] for p in sorted(changed)),
                     before=tuple(sorted(before.items())),after=tuple(sorted(after.items())))
    changes=tuple(MetadataChange(name,metadata(repo,name),data) for name,data in (
        ("ORIG_HEAD",head.hex.encode()+b"\n"),("MERGE_MSG",b"Merge "+other.hex.encode()+b"\n"),
        ("MERGE_HEAD",other.hex.encode()+b"\n")))
    context={"head":head.hex,"other":other.hex,"base":selected.hex,
             "conflicts":[{"path":c.path.hex(),"kind":c.kind} for c in sorted(conflicts.values(),key=lambda c:c.path)]}
    mutation=make_mutation(repo,checkout,"merge-begin",metadata_changes=changes,context=context)
    if clean.old_index!=checkout.old_index or clean.head!=checkout.head:raise CheckoutConflict("merge inputs changed during planning")
    objects=tuple(raw for _,raw in sorted(overlay.added.items(),key=lambda v:v[0].hex) if raw.kind is ObjectType.BLOB)
    return MergePlan(revision,head,other,selected,limits,index,tuple(sorted(conflicts.values(),key=lambda c:c.path)),mutation,objects,base)


def apply_merge(repo, plan):
    if not isinstance(plan,MergePlan):raise TypeError("select a MergePlan")
    def validate():
        if plan_merge(repo,plan.revision,base=plan.explicit_base,limits=plan.limits)!=plan:
            raise CheckoutConflict("merge inputs changed; review a fresh plan")
        for raw in plan.objects:repo.objects.write(raw)
    if plan.mutation is None:
        validate();return MergeResult("up-to-date",plan.head,plan.other)
    apply_mutation(repo,plan.mutation,validate=validate)
    return MergeResult("conflicted" if plan.conflicts else "ready",plan.head,plan.other,plan.conflicts)


def _active(repo):
    if (repo.git_dir/"operation-state").exists():raise CheckoutRecoveryRequired("operation recovery is pending")
    loaded,record,changes,oid=load_mutation(repo,state_name="merge-state")
    receipt,refs,*_=loaded
    if receipt["phase"]!="committed" or record["name"]!="merge-begin" or oid is not None:
        raise CheckoutRecoveryRequired("invalid active merge state")
    _guard(repo,receipt,refs,record,tuple(c for c in changes if c.name!="MERGE_MSG"),oid,after=True)
    context=record["context"]
    if set(context)!={"head","other","base","conflicts"}:raise CheckoutRecoveryRequired("invalid merge context")
    for key in ("head","other","base"):read_commit(repo,ObjectId(context[key],repo.object_format))
    return loaded,record


def _empty_plan(repo, loaded, index):
    receipt,refs,_,_,_,_,limits=loaded
    return CheckoutPlan(repo.git_dir,repo.refs.resolve().oid,
        False,False,refs["head_before"],receipt["source_name"],refs["source_value"],repo.refs.resolve().oid,
        receipt["source_name"],refs["source_value"],(),limits,(),(),(),_index_bytes(repo),index,
        read_file(repo.git_dir,repo.git_dir/"config",1024*1024))


def finish_merge(repo, *, author, committer, message, expected_head, expected_other, resolved=False, limits=None):
    """Publish a two-parent commit only after explicit stage-0 resolution."""
    if resolved is not True:raise CheckoutConflict("explicitly confirm conflict resolution")
    loaded,record=_active(repo);context=record["context"]
    if (expected_head,expected_other)!=(ObjectId(context["head"],repo.object_format),ObjectId(context["other"],repo.object_format)):
        raise RefConflict("merge heads differ from the expected values")
    validate_identity(author);validate_identity(committer)
    if not isinstance(message,bytes):raise TypeError("merge commit message must be bytes")
    limits=limits or HistoryLimits()
    if len(message)>limits.max_bytes:raise ObjectLimitExceeded("merge commit message exceeded")
    index_bytes=_index_bytes(repo);index=repo.index.read()
    if any(e.stage for e in index.entries):raise CheckoutConflict("resolve and stage every conflict before committing")
    tree=write_tree(repo,index,limits)
    oid=repo.objects.write(Commit.create(tree,author=author,committer=committer,message=message,parents=(expected_head,expected_other)).to_raw())
    checkout=_empty_plan(repo,loaded,index_bytes or Index(format=repo.object_format).encode())
    changes=tuple(MetadataChange(name,metadata(repo,name),None) for name in ("MERGE_HEAD","MERGE_MSG"))
    mutation=make_mutation(repo,checkout,"merge-finish",metadata_changes=changes,new_oid=oid,context={"merge_token":loaded[0]["token"]})
    def validate():
        active,_=_active(repo)
        if active[0]["token"]!=loaded[0]["token"] or _index_bytes(repo)!=index_bytes:raise CheckoutConflict("merge resolution changed before commit")
    apply_mutation(repo,mutation,validate=validate);return oid


def plan_abort_merge(repo, *, discard_tracked=False):
    if type(discard_tracked) is not bool:raise TypeError("tracked discard must be an explicit boolean")
    loaded,record=_active(repo);receipt,_,before,after,original_index,merged_index,limits=loaded
    budget=_Budget(limits);current={p:_capture(repo.work_tree,p,budget) for p in before}
    index_bytes=_index_bytes(repo)
    if not discard_tracked and (current!=after or index_bytes!=merged_index):
        raise CheckoutConflict("merge resolution changed; review explicit tracked discard before abort")
    merged=Index.from_bytes(merged_index,repo.object_format)
    now=repo.index.read();affected={c["path"] for c in record["context"]["conflicts"]}
    affected={bytes.fromhex(p) for p in affected}|set(before)
    if tuple(e for e in now.entries if e.path not in affected)!=tuple(e for e in merged.entries if e.path not in affected):
        raise CheckoutConflict("staging outside the merge would be lost by abort")
    changes=tuple(CheckoutChange(p,current[p].mode if current[p] else None,before[p].mode if before[p] else None,
                                _value_id(current[p],repo.object_format),_value_id(before[p],repo.object_format))
                  for p in sorted(before) if current[p]!=before[p])
    checkout=replace(_empty_plan(repo,loaded,original_index or Index(format=repo.object_format).encode()),
                     changes=changes,before=tuple(sorted(current.items())),after=tuple(sorted(before.items())))
    git_changes=tuple(MetadataChange(c["name"],metadata(repo,c["name"]),bytes.fromhex(c["before"]) if c["before"] is not None else None)
                      for c in record["git"])
    return make_mutation(repo,checkout,"merge-abort",metadata_changes=git_changes,
                         context={"merge_token":receipt["token"],"discard_tracked":discard_tracked},remove_index=original_index is None)


def abort_merge(repo, plan):
    if not isinstance(plan,MutationPlan) or plan.operation!="merge-abort":raise TypeError("review a merge abort plan first")
    context=json.loads(plan.context)
    def validate():
        if plan_abort_merge(repo,discard_tracked=context["discard_tracked"])!=plan:
            raise CheckoutConflict("merge abort inputs changed; review a fresh plan")
    return apply_mutation(repo,plan,validate=validate)


def recover_merge(repo):
    return recover_mutation(repo)
