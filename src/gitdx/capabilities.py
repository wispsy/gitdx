"""Read-only capability profiles with explicitly selected qualification evidence."""

from dataclasses import asdict, dataclass, fields
from enum import StrEnum
import hashlib
from importlib import metadata, resources
import json
import platform
import re
import ssl
import sys


class CapabilityState(StrEnum):
    SUPPORTED='supported'
    TESTED='tested'
    UNSUPPORTED='unsupported'
    EXPERIMENTAL='experimental'


# A tested record names the actual fixture checks required for that scope.
_PROFILES=(
    ('objects.native', 'supported', 'Native SHA-1/SHA-256 byte codecs', ('native-objects',)),
    ('storage.loose', 'supported', 'Verified loose objects and immutable writes', ('write-after-gc',)),
    ('storage.pack-index-v2', 'supported', 'Indexed native packs after Git gc', ('packed-read',)),
    ('storage.delta-ofs-ref', 'supported', 'Bounded indexed OFS and REF deltas', ('delta-ofs','delta-ref')),
    ('refs.files-packed', 'supported', 'Loose/packed refs with single-ref CAS', ('packed-refs','ref-cas')),
    ('refs.coordinated-multi-update', 'supported', 'All-value CAS under deterministic files-ref locks with explicit rollback/finalize recovery; no atomic native visibility', ('refs-transaction-native','refs-transaction-fault','refs-transaction-contention','refs-transaction-no-git')),
    ('worktree.index-stage-status', 'supported', 'Portable index, add and status', ('write-after-gc','native-status')),
    ('worktree.checkout', 'supported', 'Reviewed branch/detached/unborn checkout', ('checkout',)),
    ('worktree.checkout-recovery', 'supported', 'Guarded process-interruption recovery', ('checkout-fault',)),
    ('worktree.merge-two-parent', 'supported', 'Explicit no-ff merge plans, conflict stages, guarded finish/abort and recovery',
     ('merge-native','merge-conflicts','merge-recovery','merge-no-git')),
    ('worktree.reset-restore', 'supported', 'Explicit soft/mixed/hard reset and literal-path index/worktree restoration',
     ('reset-native','restore-native','reset-restore-recovery','reset-restore-no-git')),
    ('history.commit', 'supported', 'Explicit identities and expected-HEAD commits', ('write-after-gc',)),
    ('diff.trees-bytes', 'supported', 'Bounded explicit-tree byte diffs, LF hunks and optional deterministic renames', ('diff-native',)),
    ('history.revisions', 'supported', 'Explicit refs/OID prefixes, parent suffixes and typed tag peeling', ('revision-native',)),
    ('history.ancestry', 'supported', 'Iterative two-root ancestry and all deterministic best common ancestors', ('ancestry-native',)),
    ('history.replacements', 'unsupported', 'Revision/ancestry queries refuse graft and replacement metadata', ('replacement-refusal',)),
    ('worktree.index-required', 'unsupported', 'Non-v2, split/sparse and unknown required index features are refused', ('refusal-index-required',)),
    ('transfer.fetch-local-http', 'supported', 'Complete same-format local and smart-HTTP fetch/clone', ('fetch-interop',)),
    ('protocol.push-records', 'supported', 'Bounded receive-pack commands and report-status v1/v2 records', ('push-codec',)),
    ('protocol.upload-pack', 'supported', 'Bounded read-only stateless v0/v1/v2 discovery/fetch with complete advertised-tip packs', ('upload-pack-native','upload-pack-policy','upload-pack-no-git')),
    ('receive.quarantine-policy', 'supported', 'Bounded complete object graphs, explicit policy and single-ref receive outcomes', ('receive-native','receive-policy','receive-fault','receive-no-git')),
    ('config.local', 'supported', 'Local ordered configuration and conservative worktree policy', ('native-status','refusal-profiles')),
    ('config.includes-inspection', 'experimental', 'Explicit confined read-only includes; repository mutations refuse includes', ()),
    ('repository.alternates', 'unsupported', 'Alternates are refused before repository operations', ('refusal-profiles',)),
    ('repository.shallow', 'unsupported', 'Shallow boundaries are refused; no implicit completion', ('refusal-profiles',)),
    ('repository.promisor', 'unsupported', 'Promisor/filter metadata is refused; no lazy fetch', ('refusal-profiles',)),
    ('repository.required-unknown', 'unsupported', 'Unknown required formats/extensions fail before writes', ('refusal-profiles',)),
    ('repository.reftable', 'unsupported', 'Files refs only; reftable is a later profile', ('refusal-profiles',)),
    ('repository.linked-worktrees', 'unsupported', 'Gitfiles/commondir and linked worktrees are refused', ()),
    ('attributes.conversion', 'unsupported', 'External filters, text conversion and encoding drivers never execute', ()),
    ('refs.atomic-multi-update', 'unsupported', 'Single-ref transactions do not promise atomic multi-ref visibility', ()),
    ('transfer.push-local', 'supported', 'Explicit branch/tag pushes to bare local destinations with individual CAS outcomes', ('push-local',)),
    ('transfer.push-http', 'supported', 'Same-format smart-HTTP push with explicit auth and per-ref reports', ('push-http',)),
    ('server.git-routes', 'unsupported', 'Production upload/receive handlers are separately scoped', ()),
    ('objects.cross-format', 'unsupported', 'Object-format advertisement never negotiates conversion', ()),
)


def implementation_digest():
    """Bind evidence to installed Python module bytes, without invoking Git."""
    digest=hashlib.sha256();root=resources.files('gitdx')
    for entry in sorted(root.iterdir(),key=lambda p:p.name):
        if entry.name.endswith('.py'):
            digest.update(entry.name.encode()+b'\0');digest.update(entry.read_bytes());digest.update(b'\0')
    return digest.hexdigest()


def runtime_toolchain():
    return {'python':sys.version,'implementation':sys.implementation.name,'platform':sys.platform,
            'machine':platform.machine(),'openssl':ssl.OPENSSL_VERSION,'package':metadata.version('gitdx')}


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    implementation_digest: str
    profile: str
    toolchain: dict
    checks: tuple[str,...]
    capabilities: tuple[str,...]
    formats: tuple[str,...] = ('sha1','sha256')
    source_revision: str | None = None
    artifact_sha256: str | None = None
    oracle_required: bool = True
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version!=1 or self.oracle_required is not True:raise ValueError('evidence requires schema 1 and a mandatory oracle')
        if self.profile not in ('source','wheel','sdist'):raise ValueError('unknown qualification profile')
        for value in (self.implementation_digest,self.artifact_sha256):
            if value is not None and (not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)):
                raise ValueError('invalid qualification digest')
        if self.implementation_digest is None:raise ValueError('implementation digest is required')
        if self.source_revision is not None and (not isinstance(self.source_revision,str) or not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})',self.source_revision)):
            raise ValueError('invalid source revision')
        if self.profile!='source' and self.artifact_sha256 is None:raise ValueError('installed evidence requires artifact identity')
        required=set(runtime_toolchain())|{'git','uv'}
        if not isinstance(self.toolchain,dict) or set(self.toolchain)!=required or any(not isinstance(v,str) or not 1<=len(v)<=1024 for v in self.toolchain.values()):
            raise ValueError('qualification requires exact runtime, Git and uv versions')
        object.__setattr__(self,'toolchain',dict(self.toolchain))
        for key in ('checks','capabilities','formats'):
            values=getattr(self,key)
            if not isinstance(values,(tuple,list)) or len(values)>128 or any(not isinstance(v,str) or not re.fullmatch('[a-z0-9.-]{1,96}',v) for v in values) or len(set(values))!=len(values):
                raise ValueError('invalid qualification records')
            object.__setattr__(self,key,tuple(values))
        if set(self.formats)!={'sha1','sha256'}:raise ValueError('qualification must cover both native formats')
        supported={key:checks for key,state,_,checks in _PROFILES if state=='supported'}
        for key in self.capabilities:
            if key not in supported or not set(supported[key])<=set(self.checks):raise ValueError('capability has no matching fixture evidence')

    def to_json(self):return json.dumps(asdict(self),sort_keys=True,indent=2)+'\n'

    @classmethod
    def from_json(cls,data):
        if not isinstance(data,(str,bytes)) or len(data)>1024*1024:raise ValueError('qualification evidence exceeds its limit')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('duplicate evidence field')
                result[key]=value
            return result
        try:
            value=json.loads(data,object_pairs_hook=unique)
            if not isinstance(value,dict) or set(value)!={f.name for f in fields(cls)}:raise ValueError('invalid evidence fields')
            return cls(**value)
        except (TypeError,UnicodeError,json.JSONDecodeError):raise ValueError('invalid qualification evidence') from None


def capability_report(evidence=None):
    """Describe the implementation; only matching selected evidence says tested."""
    runtime=runtime_toolchain();fingerprint=implementation_digest()
    if evidence is not None:
        if not isinstance(evidence,QualificationEvidence):raise TypeError('select QualificationEvidence explicitly')
        # Revalidate mutable toolchain data as well as module/runtime identity.
        evidence=QualificationEvidence.from_json(evidence.to_json())
        if evidence.implementation_digest!=fingerprint or any(evidence.toolchain[k]!=v for k,v in runtime.items()):
            raise ValueError('qualification evidence does not match this runtime implementation')
    selected=set(evidence.capabilities) if evidence else set()
    checks=set(evidence.checks) if evidence else set()
    return {'schema_version':1,'implementation_digest':fingerprint,'runtime':runtime,
            'qualification':asdict(evidence) if evidence else None,
            'capabilities':[{'id':key,'state':'tested' if key in selected else state,'scope':scope,
                             'evidence_checks':[c for c in required if c in checks]}
                            for key,state,scope,required in _PROFILES]}
