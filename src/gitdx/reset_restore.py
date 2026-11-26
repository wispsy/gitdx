"""Explicit ref/index/worktree previews with shared journaled publication."""

from dataclasses import dataclass, replace

from .checkout import (CheckoutChange, CheckoutConflict, CheckoutLimits, CheckoutPlan,
                       _Budget, _index_bytes, _parents, _plan_checkout, _profile,
                       _tree, _value_id, plan_checkout)
from .errors import InvalidObject, UnsupportedFormat
from .filesystem import read_file
from .history import read_commit
from .index import Index, IndexEntry
from .merge import _Overlay
from .object_store import ObjectLimitExceeded
from .objects import Commit, ObjectId, ObjectType
from .refs import DirectRef, RefConflict, SymbolicRef
from .working_tree import WorkingTree, portable_path
from .worktree_transaction import (MetadataChange, MutationPlan, apply_mutation,
                                   make_mutation, metadata)
from .history import HistoryLimits, write_tree

PLAN_IDENTITY=b"Restore plan <plan@example.invalid> 0 +0000"


@dataclass(frozen=True, slots=True)
class ResetPlan:
    revision: str | ObjectId
    mode: str
    expected_head: ObjectId
    target: ObjectId
    discard_staged: bool
    overwrite_tracked: bool
    limits: CheckoutLimits
    index_changes: tuple[CheckoutChange, ...]
    mutation: MutationPlan

    @property
    def worktree_changes(self):return self.mutation.changes

    @property
    def ref_name(self):return self.mutation.checkout.source_name


@dataclass(frozen=True, slots=True)
class RestorePlan:
    paths: tuple[bytes, ...]
    source: str | ObjectId | None
    source_oid: ObjectId | None
    staged: bool
    worktree: bool
    discard_staged: bool
    overwrite_tracked: bool
    limits: CheckoutLimits
    index_changes: tuple[CheckoutChange, ...]
    mutation: MutationPlan

    @property
    def worktree_changes(self):return self.mutation.changes


def _entries(index):return {e.path:(e.oid,e.mode) for e in index.entries}


def _index_changes(before,after):
    return tuple(CheckoutChange(p,before[p][1] if p in before else None,after[p][1] if p in after else None,
                                before[p][0] if p in before else None,after[p][0] if p in after else None)
                 for p in sorted(set(before)|set(after)) if before.get(p)!=after.get(p))


def _index(repo):
    value=repo.index.read()
    if any(e.stage or e.mode==0o160000 for e in value.entries):raise UnsupportedFormat("reset/restore require a resolved non-submodule index")
    return value


def _tree_index(repo, oid, limits):
    budget=_Budget(limits);values=_tree(repo,oid,limits,budget)
    for path,(object_id,_) in values.items():
        raw=repo.objects.read(object_id)
        if raw.kind is not ObjectType.BLOB:raise InvalidObject("reset/restore file must name a blob")
        budget.add(len(raw.data))
    return values


def _reference_plan(repo, new_index, limits):
    refs=repo.refs;head=refs.read("HEAD");source=refs.resolve()
    if not isinstance(head,(DirectRef,SymbolicRef)):raise InvalidObject("HEAD must exist")
    if isinstance(head,SymbolicRef) and (source.symbolic_chain!=("HEAD",) or not head.target.startswith("refs/heads/") or source.name!=head.target):
        raise UnsupportedFormat("mutation requires detached HEAD or a direct branch symbolic HEAD")
    value=refs.read(source.name)
    if value!=(DirectRef(source.oid) if source.oid else None):raise RefConflict("source ref changed while planning")
    if source.oid is not None:refs._target(source.name,source.oid)
    result=CheckoutPlan(repo.git_dir,source.oid,False,False,head,source.name,value,source.oid,source.name,value,(),limits,
                        (),(),(),_index_bytes(repo),new_index,read_file(repo.git_dir,repo.git_dir/"config",1024*1024))
    if refs.read("HEAD")!=head or refs.read(source.name)!=value:raise RefConflict("HEAD changed during planning")
    return result


def plan_reset(repo, revision, *, mode, expected_head, discard_staged=False, overwrite_tracked=False, limits=None):
    """Preview soft/mixed/hard effects; dirty staging/worktree loss is explicit."""
    _profile(repo);limits=limits or CheckoutLimits()
    if mode not in ("soft","mixed","hard"):raise UnsupportedFormat("select soft, mixed or hard reset explicitly")
    if any(type(v) is not bool for v in (discard_staged,overwrite_tracked)):raise TypeError("discard options must be explicit booleans")
    if (mode!="mixed" and discard_staged) or (mode!="hard" and overwrite_tracked):raise ValueError("discard option does not apply to the selected reset mode")
    current=repo.refs.resolve().oid
    if current!=expected_head:raise RefConflict("HEAD differs from its expected value")
    if current is None:raise UnsupportedFormat("reset requires an existing HEAD commit")
    read_commit(repo,current);target=repo.resolve(revision);read_commit(repo,target)
    wanted=_tree_index(repo,target,limits);old=_index(repo);old_values=_entries(old)
    if mode=="soft":
        raw=_index_bytes(repo);checkout=_reference_plan(repo,raw or Index(format=repo.object_format).encode(),limits)
        index_after=old_values
    elif mode=="mixed":
        original=_tree(repo,current,limits,_Budget(limits))
        if old_values!=original and not discard_staged:raise CheckoutConflict("mixed reset would discard staging; select discard_staged explicitly")
        index_after=wanted
        encoded=Index(tuple(IndexEntry(p,oid,m) for p,(oid,m) in wanted.items()),repo.object_format).encode(repo.index.limits)
        checkout=_reference_plan(repo,encoded,limits)
    else:
        checkout=plan_checkout(repo,target,overwrite_tracked=overwrite_tracked,limits=limits)
        index_after=wanted
    changes=(MetadataChange("ORIG_HEAD",metadata(repo,"ORIG_HEAD"),current.hex.encode()+b"\n"),)
    mutation=make_mutation(repo,checkout,"reset",metadata_changes=changes,new_oid=target,context={"mode":mode},
                           remove_index=mode=="soft" and checkout.old_index is None)
    return ResetPlan(revision,mode,expected_head,target,discard_staged,overwrite_tracked,limits,_index_changes(old_values,index_after),mutation)


def apply_reset(repo, plan):
    if not isinstance(plan,ResetPlan):raise TypeError("review a ResetPlan first")
    def validate():
        fresh=plan_reset(repo,plan.revision,mode=plan.mode,expected_head=plan.expected_head,
                         discard_staged=plan.discard_staged,overwrite_tracked=plan.overwrite_tracked,limits=plan.limits)
        if fresh!=plan:raise CheckoutConflict("reset inputs changed; review a fresh plan")
    return apply_mutation(repo,plan.mutation,validate=validate)


def _selected(path,selectors):return b"" in selectors or path in selectors or bool(_parents(path)&selectors)


def _virtual_target(repo,values,limits):
    overlay=_Overlay(repo.objects);virtual=replace(repo,objects=overlay)
    index=Index(tuple(IndexEntry(p,oid,m) for p,(oid,m) in values.items()),repo.object_format)
    tree=write_tree(virtual,index,HistoryLimits(max_paths=limits.max_paths,max_depth=limits.max_depth,max_bytes=limits.max_bytes))
    oid=overlay.write(Commit.create(tree,author=PLAN_IDENTITY,committer=PLAN_IDENTITY,message=b"Private restore plan\n").to_raw())
    return virtual,oid


def plan_restore(repo, paths, *, source=None, staged=False, worktree=True, discard_staged=False, overwrite_tracked=False, limits=None):
    """Preview literal selected paths with independent index/worktree effects."""
    _profile(repo);limits=limits or CheckoutLimits()
    if any(type(v) is not bool for v in (staged,worktree,discard_staged,overwrite_tracked)):raise TypeError("restore options must be explicit booleans")
    if not staged and not worktree:raise ValueError("select index and/or worktree restoration")
    if discard_staged and not staged or overwrite_tracked and not worktree:raise ValueError("discard option does not apply to the selected restore effects")
    selectors=frozenset(portable_path(p) for p in paths)
    if not selectors:raise ValueError("select at least one literal path")
    if len(selectors)>limits.max_paths:raise ObjectLimitExceeded("restore selection count exceeded")
    old=_index(repo);original=_entries(old)
    source_oid=None
    if source is None and not staged:wanted=original
    else:
        source_oid=repo.resolve("HEAD" if source is None else source);read_commit(repo,source_oid)
        wanted=_tree_index(repo,source_oid,limits)
    candidates=set(original)|set(wanted)
    for selector in selectors:
        if not any(_selected(p,{selector}) for p in candidates):raise FileNotFoundError("restore selection has no indexed or source paths")
    index_after=original.copy()
    if staged:
        for p in tuple(index_after):
            if _selected(p,selectors):del index_after[p]
        index_after.update((p,v) for p,v in wanted.items() if _selected(p,selectors))
        indexed_paths=set(index_after)
        if any(_parents(p)&indexed_paths for p in indexed_paths):raise InvalidObject("restore would create an index file/directory collision")
    changes=_index_changes(original,index_after)
    if staged and changes and not discard_staged:
        head_values=_tree(repo,repo.refs.resolve().oid,limits,_Budget(limits))
        if any(original.get(c.path)!=head_values.get(c.path) for c in changes):
            raise CheckoutConflict("restore would discard staging; select discard_staged explicitly")
    by_path={e.path:e for e in old.entries}
    entries=tuple(by_path[p] if original.get(p)==value else IndexEntry(p,value[0],value[1]) for p,value in sorted(index_after.items()))
    updated=old.with_entries(entries);old_bytes=_index_bytes(repo)
    encoded=old_bytes if updated is old else updated.encode(repo.index.limits)
    encoded_payload=encoded or Index(format=repo.object_format).encode(repo.index.limits)
    if worktree:
        virtual,target=_virtual_target(repo,wanted,limits)
        checkout=_plan_checkout(virtual,target,overwrite_tracked=True,limits=limits,selectors=selectors)
        working=WorkingTree(repo);before,after=dict(checkout.before),dict(checkout.after)
        for path,(oid,mode) in original.items():
            if not _selected(path,selectors) or before.get(path)==after.get(path):continue
            value=before.get(path);physical=value.mode if value else None
            if value and not working.filemode and physical in (0o100644,0o100755):physical=mode
            if value and not working.symlinks and mode==0o120000 and physical in (0o100644,0o100755):physical=mode
            if not overwrite_tracked and (physical!=mode or _value_id(value,repo.object_format)!=oid):
                raise CheckoutConflict("restore would discard working changes; select overwrite_tracked explicitly")
        for path in wanted:
            if _selected(path,selectors) and path not in original and before.get(path) is not None:
                raise CheckoutConflict("restore would overwrite untracked content")
        checkout=replace(checkout,new_index=encoded_payload)
    else:checkout=_reference_plan(repo,encoded_payload,limits)
    mutation=make_mutation(repo,checkout,"restore",context={},remove_index=encoded is None)
    return RestorePlan(tuple(p or b"." for p in sorted(selectors)),source,source_oid,staged,worktree,discard_staged,overwrite_tracked,limits,changes,mutation)


def apply_restore(repo,plan):
    if not isinstance(plan,RestorePlan):raise TypeError("review a RestorePlan first")
    def validate():
        fresh=plan_restore(repo,plan.paths,source=plan.source,staged=plan.staged,worktree=plan.worktree,
                           discard_staged=plan.discard_staged,overwrite_tracked=plan.overwrite_tracked,limits=plan.limits)
        if fresh!=plan:raise CheckoutConflict("restore inputs changed; review a fresh plan")
    return apply_mutation(repo,plan.mutation,validate=validate)
