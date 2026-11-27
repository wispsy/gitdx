"""Mandatory native Git qualification, plus a separate no-Git runtime worker."""

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import gitdx
from gitdx import (Blob, Commit, DirectRef, GitdxError, Index, InvalidIndex, ObjectFormat, ObjectId, PackFile, PackIndex, PushState,
                   PushUpdate, QualificationEvidence, RefConflict, Repository, SmartHTTPTransport, TransferLimits,
                   UnsupportedFormat, capability_report, implementation_digest, runtime_toolchain, LocalTransport,
                   PushCommand, ReceivePolicy, RefChange, IngestionProfile)
from gitdx.confined_fs import supported as confined_storage_supported

IDENTITY=b'Qualification <qualification@example.invalid> 1700000000 +0000'
CHECKS=('native-objects', 'packed-read', 'delta-ofs', 'delta-ref', 'packed-refs', 'ref-cas', 'write-after-gc',
 'native-status', 'checkout', 'refusal-profiles', 'no-git-runtime', 'diff-native', 'revision-native',
 'ancestry-native', 'fetch-interop', 'fetch-v0', 'fetch-v1', 'fetch-v2', 'push-codec', 'push-local',
 'push-http', 'report-status-v1', 'report-status-v2', 'native-client-storage', 'native-index-pack',
 'corruption-resource', 'index-v2', 'refusal-index-required', 'replacement-refusal', 'merge-native',
 'merge-conflicts', 'merge-recovery', 'merge-no-git', 'reset-native', 'restore-native',
 'reset-restore-recovery', 'reset-restore-no-git', 'receive-native', 'receive-policy', 'receive-fault',
 'receive-no-git', 'refs-transaction-native', 'refs-transaction-fault', 'refs-transaction-contention',
 'refs-transaction-no-git', 'ingestion-policy', 'ingestion-native', 'ingestion-no-git')
if confined_storage_supported():CHECKS+=('ingestion-confinement',)
CAPABILITIES=('objects.native', 'storage.loose', 'storage.pack-index-v2', 'storage.delta-ofs-ref',
 'refs.files-packed', 'worktree.index-stage-status', 'worktree.checkout', 'history.commit',
 'config.local', 'diff.trees-bytes', 'history.revisions', 'history.ancestry',
 'transfer.fetch-local-http', 'protocol.push-records', 'transfer.push-local', 'transfer.push-http',
 'worktree.merge-two-parent', 'worktree.reset-restore', 'receive.quarantine-policy',
 'refs.coordinated-multi-update', 'receive.ingestion-validation')
if confined_storage_supported():CAPABILITIES+=('receive.untrusted-sha256',)


def git(path,*args,data=None):
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0',
               GIT_AUTHOR_NAME='Qualification',GIT_AUTHOR_EMAIL='qualification@example.invalid',
               GIT_COMMITTER_NAME='Qualification',GIT_COMMITTER_EMAIL='qualification@example.invalid',
               GIT_AUTHOR_DATE='1700000000 +0000',GIT_COMMITTER_DATE='1700000000 +0000')
    return subprocess.run(['git','-C',str(path),'-c','commit.gpgsign=false','-c','tag.gpgsign=false',*args],
                          input=data,capture_output=True,env=env,check=True).stdout


def snapshot(root):
    return {str(p.relative_to(root)):(p.stat().st_mode&0o777,hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


def native_objects(repo):
    ids=[line.split(b' ',1)[0] for line in git(repo,'rev-list','--objects','--all').splitlines()]
    data=git(repo,'cat-file','--batch',data=b'\n'.join(ids)+b'\n');cursor=0;result=[]
    for expected in ids:
        end=data.index(b'\n',cursor);oid,kind,size=data[cursor:end].split();size=int(size);cursor=end+1
        payload=data[cursor:cursor+size];cursor+=size+1
        if oid!=expected or len(payload)!=size:raise RuntimeError('invalid native object oracle response')
        result.append((oid.decode(),kind.decode(),hashlib.sha256(payload).hexdigest()))
    if cursor!=len(data):raise RuntimeError('trailing native object oracle data')
    return result


def make_fixture(root,format):
    source=root/'repo';git(root,'init','--template=','--initial-branch=main','--object-format='+format.value,str(source))
    git(source,'config','user.name','Qualification');git(source,'config','user.email','qualification@example.invalid')
    git(source,'config','core.autocrlf','false');git(source,'config','index.version','2')
    (source/'.gitattributes').write_bytes(b'* -text -ident\n');(source/'nested').mkdir()
    (source/'nested/data').write_bytes(b'binary\0\xff\r\n')
    for n in range(4):
        (source/'file').write_bytes(b'shared contents\n'*1000+str(n).encode()+b'tail\n'*500)
        git(source,'add','.');git(source,'commit','-m','native '+str(n))
    tip=git(source,'rev-parse','HEAD').strip().decode();old=git(source,'rev-parse','HEAD~2').strip().decode()
    git(source,'branch','older',old);git(source,'tag','-a','qualified','-m','native tag')
    tree=git(source,'rev-parse','HEAD^{tree}').strip().decode()
    def node(name,parents=()):
        args=['commit-tree',tree]
        for parent in parents:args.extend(('-p',parent))
        return git(source,*args,data=name.encode()+b'\n').strip().decode()
    a=node('a',(tip,));b=node('b',(tip,));left=node('left',(a,b));right=node('right',(b,a));other=node('disconnected')
    for name,oid in (('graph-left',left),('graph-right',right),('disconnected',other)):git(source,'branch',name,oid)
    queries=('HEAD','qualified','qualified^{}','qualified^{commit}','qualified^{tree}','HEAD~2','graph-left^2','graph-right~2')
    revisions={q:git(source,'rev-parse','--verify',q).strip().decode() for q in queries}
    bases=git(source,'merge-base','--all',left,right).splitlines()
    diff_old=git(source,'rev-parse','HEAD~1^{tree}').strip().decode()
    diff_new=tree;raw=git(source,'diff-tree','-r','--raw','-z','--no-abbrev','--no-renames',diff_old,diff_new).split(b'\0')
    diffs=[]
    for i in range(0,len(raw)-1,2):
        am,bm,ao,bo,status=raw[i][1:].split()
        diffs.append((raw[i+1].hex(),int(am,8),int(bm,8),ao.decode(),bo.decode(),status.decode()))
    git(source,'gc','--prune=now');git(source,'pack-refs','--all')
    if not list((source/'.git/objects/pack').glob('*.pack')) or not (source/'.git/packed-refs').is_file():
        raise RuntimeError('native gc did not produce packed objects/refs')
    objects=native_objects(source);repo=Repository.open(source);indexed=[]
    raws=[Blob(b'shared contents\n'*1000+str(n).encode()+b'tail\n'*500).to_raw() for n in range(20)]
    ids=[repo.objects.write(raw) for raw in raws]
    for ofs in (False,True):
        folder=root/('ofs' if ofs else 'ref');folder.mkdir();prefix=folder/'pack'
        args=['pack-objects','--window=20','--depth=20']
        if ofs:args.append('--delta-base-offset')
        checksum=git(source,*args,str(prefix),data=b''.join(str(oid).encode()+b'\n' for oid in ids)).strip().decode()
        path=Path(str(prefix)+'-'+checksum)
        verified=git(source,'verify-pack','-v',str(path.with_suffix('.idx')))
        if not any(len(line.split())==7 for line in verified.splitlines()):raise RuntimeError('native delta fixture was not generated')
        indexed.append({'pack':str(path.with_suffix('.pack')),'index':str(path.with_suffix('.idx')),
                        'kind':6 if ofs else 7,'objects':[(str(oid),hashlib.sha256(raw.data).hexdigest()) for oid,raw in zip(ids,raws)]})
    return {'root':str(source),'format':format.value,'tip':tip,'old':old,'objects':objects,'indexed':indexed,
            'revisions':revisions,'bases':[oid.decode() for oid in bases],'left':left,'right':right,'disconnected':other,
            'diff_old':diff_old,'diff_new':diff_new,'diffs':diffs}


def runtime(spec):
    if os.environ.get('PATH')!='':raise RuntimeError('runtime worker requires Git absent from PATH')
    root=Path(spec['root']);repo=Repository.open(root);format=ObjectFormat(spec['format'])
    if repo.object_format!=format:raise RuntimeError('native repository format mismatch')
    for hex,kind,digest in spec['objects']:
        raw=repo.objects.read(ObjectId(hex,format))
        if raw.kind.value!=kind or hashlib.sha256(raw.data).hexdigest()!=digest:raise RuntimeError('packed native object mismatch')
    for record in spec['indexed']:
        pack=PackFile(Path(record['pack']).read_bytes(),PackIndex.from_bytes(Path(record['index']).read_bytes(),format))
        if not any(e.kind==record['kind'] for e in pack._entries.values()):raise RuntimeError('expected native delta kind absent')
        for hex,digest in record['objects']:
            if hashlib.sha256(pack.read(ObjectId(hex,format)).data).hexdigest()!=digest:raise RuntimeError('native delta reconstruction mismatch')
    tip=ObjectId(spec['tip'],format)
    if repo.refs.resolve().oid!=tip or repo.status():raise RuntimeError('native packed/index/status mismatch')
    for query,expected in spec['revisions'].items():
        if str(repo.resolve(query))!=expected:raise RuntimeError('native revision mismatch')
    if {str(oid) for oid in repo.merge_bases(spec['left'],spec['right'])}!=set(spec['bases']):raise RuntimeError('native criss-cross bases mismatch')
    if not repo.is_ancestor(tip,spec['left']) or repo.is_ancestor(spec['disconnected'],spec['right']):raise RuntimeError('native ancestry mismatch')
    if repo.merge_bases(spec['left'],spec['disconnected']):raise RuntimeError('disconnected native bases mismatch')
    records=[]
    for c in repo.diff(ObjectId(spec['diff_old'],format),ObjectId(spec['diff_new'],format)):
        records.append(((c.new_path or c.old_path).hex(),c.old_mode or 0,c.new_mode or 0,
                        str(c.old_oid) if c.old_oid else '0'*format.hex_size,str(c.new_oid) if c.new_oid else '0'*format.hex_size,c.status))
    if records!=[tuple(r) for r in spec['diffs']]:raise RuntimeError('native raw diff mismatch')
    repo.refs.update('refs/heads/qualification',tip,expected=None)
    try:repo.refs.update('refs/heads/qualification',tip,expected=None)
    except RefConflict:pass
    else:raise RuntimeError('create-only ref CAS did not refuse')
    (root/'qualified.bin').write_bytes(b'exact\0\xff\r\n$Id$');repo.add('qualified.bin')
    new=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'write after gc\n',expected_head=tip)
    repo.refs.update('refs/heads/qualification',new,expected=tip)
    repo.apply_checkout(repo.plan_checkout('older'))
    if (root/'qualified.bin').exists() or repo.refs.resolve().oid!=ObjectId(spec['old'],format):raise RuntimeError('packed checkout mismatch')
    repo.apply_checkout(repo.plan_checkout('main'))
    if repo.status() or (root/'qualified.bin').read_bytes()!=b'exact\0\xff\r\n$Id$':raise RuntimeError('post-gc write/status mismatch')
    runtime_merge(root.parent/'merge-profile',format)
    runtime_reset_restore(root.parent/'reset-profile',format)
    transfers=runtime_transfers(repo,spec,new)
    runtime_receive(repo,root.parent,ObjectId(str(new),format))
    print(json.dumps({'new':str(new),'checks':CHECKS,'transfers':transfers}))


def runtime_merge(path,format):
    """The same merge implementation must run with Git absent from PATH."""
    repo=Repository.init(path,object_format=format)
    file=path/'file';base=b'one\ntwo\nthree\nfour\nfive\n';file.write_bytes(base);repo.add('file')
    original=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'base',expected_head=None)
    repo.refs.update('refs/heads/side',original,expected=None)
    file.write_bytes(base.replace(b'one',b'OURS'));repo.add('file')
    ours=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'ours',expected_head=original)
    repo.apply_checkout(repo.plan_checkout('side'))
    file.write_bytes(base.replace(b'five',b'THEIRS'));repo.add('file')
    theirs=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'theirs',expected_head=original)
    repo.apply_checkout(repo.plan_checkout('main'))
    if repo.apply_merge(repo.plan_merge('side')).state!='ready':raise RuntimeError('no-Git merge did not prepare')
    oid=repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b'merge',expected_head=ours,expected_other=theirs,resolved=True)
    if Commit.from_bytes(repo.objects.read(oid).data,format).parents!=(ours,theirs) or repo.status():
        raise RuntimeError('no-Git merge publication mismatch')
    if file.read_bytes()!=base.replace(b'one',b'OURS').replace(b'five',b'THEIRS'):raise RuntimeError('no-Git merge content mismatch')


def runtime_reset_restore(path,format):
    repo=Repository.init(path,object_format=format);file=path/'file'
    file.write_bytes(b'old');repo.add('file')
    old=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'old',expected_head=None)
    file.write_bytes(b'new');repo.add('file')
    new=repo.commit(author=IDENTITY,committer=IDENTITY,message=b'new',expected_head=old)
    before=(repo.git_dir/'index').read_bytes()
    repo.apply_reset(repo.plan_reset(old,mode='soft',expected_head=new))
    if (repo.git_dir/'index').read_bytes()!=before or file.read_bytes()!=b'new':raise RuntimeError('no-Git soft reset changed index/worktree')
    repo.apply_reset(repo.plan_reset(new,mode='hard',expected_head=old,overwrite_tracked=True))
    file.write_bytes(b'dirty')
    repo.apply_restore(repo.plan_restore('file',overwrite_tracked=True))
    if file.read_bytes()!=b'new' or repo.refs.resolve().oid!=new:raise RuntimeError('no-Git restore changed the wrong state')
    repo.apply_restore(repo.plan_restore('file',source=old,staged=True,worktree=False))
    repo.apply_reset(repo.plan_reset(old,mode='mixed',expected_head=new,discard_staged=True))
    if file.read_bytes()!=b'new' or repo.refs.resolve().oid!=old:raise RuntimeError('no-Git mixed reset mismatch')


def runtime_receive(repo,root,new):
    target=Repository.init(root/'receive-no-git.git',bare=True,object_format=repo.object_format)
    command=PushCommand(b'refs/heads/main',None,new);tag=PushCommand(b'refs/tags/current',None,new);pack=repo.pack((new,)).pack
    denied=target.receive((command,),pack,policy=ReceivePolicy(ref_check=lambda view,c:b'policy refused'))
    if denied.complete or target.refs.resolve().oid is not None:raise RuntimeError('receive policy failed to refuse')
    result=target.receive((command,tag),pack,policy=ReceivePolicy(),coordinated=True)
    if not result.complete or result.atomic or result.cleanup_pending:raise RuntimeError('no-Git receive did not complete')
    if target.refs.resolve().oid!=new or target.objects.read(new)!=repo.objects.read(new):raise RuntimeError('no-Git receive object/ref mismatch')
    if not result.coordinated or not result.ref_transaction.committed:raise RuntimeError('coordinated receive did not commit')
    untrusted=Repository.init(root/'untrusted-no-git.git',bare=True,object_format=repo.object_format)
    policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED)
    if repo.object_format is ObjectFormat.SHA256 and confined_storage_supported():
        result=untrusted.receive((command,),pack,policy=policy,coordinated=True)
        if not result.complete or untrusted.refs.resolve().oid!=new:raise RuntimeError('untrusted no-Git receive failed')
    else:
        try:untrusted.receive((command,),pack,policy=policy)
        except UnsupportedFormat:pass
        else:raise RuntimeError('unsupported untrusted profile was accepted')
        if not untrusted.refs.resolve().unborn:raise RuntimeError('ref changed on ingestion refusal')
    result=target.refs.transaction((RefChange('refs/tags/current',None,new),RefChange('refs/heads/main',new,new)))
    if not result.committed or result.atomic or target.refs.read('refs/tags/current') is not None:
        raise RuntimeError('no-Git ref transaction did not preserve its guarantees')


def runtime_transfers(repo,spec,new):
    root=repo.git_dir.parent.parent;format=repo.object_format;tip=ObjectId(spec['tip'],format)
    client=Repository.clone(LocalTransport(repo),root/'local-client')
    if client.refs.resolve().oid!=new or client.status():raise RuntimeError('local clone mismatch')
    if not client.fetch(LocalTransport(repo),tags=True).complete:raise RuntimeError('local fetch mismatch')
    destination=Repository.init(root/'local-receiver.git',bare=True,object_format=format)
    before=snapshot(repo.git_dir)
    for proposed,expected in ((tip,None),(new,tip)):
        result=repo.push(LocalTransport(destination),(PushUpdate('refs/heads/main',proposed,expected_old=expected),))
        if not result.complete or result.peer_atomic or result.atomic:raise RuntimeError('local individual push mismatch')
    result=repo.push(LocalTransport(destination),(PushUpdate('refs/heads/main',new,expected_old=None),))
    if result.outcomes[0].state is not PushState.REJECTED:raise RuntimeError('local stale push accepted')
    for proposed,expected in ((new,None),(None,new)):
        if not repo.push(LocalTransport(destination),(PushUpdate('refs/tags/temporary',proposed,expected_old=expected),)).complete:
            raise RuntimeError('local tag create/delete mismatch')
    if snapshot(repo.git_dir)!=before:raise RuntimeError('local push mutated sender')
    (root/'gitdx-output.pack').write_bytes(repo.pack((new,)).pack)
    endpoint=spec['http']+'/http-source.git';current=tip
    for version in (0,1,2):
        transport=SmartHTTPTransport(endpoint,allow_http=True,protocol_version=version)
        fetched=Repository.clone(transport,root/('http-client-'+str(version)))
        if fetched.refs.resolve().oid!=current or fetched.status():raise RuntimeError('HTTP clone mismatch')
        tree=Commit.from_bytes(repo.objects.read(current).data,format).tree
        next_oid=repo.objects.write(Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=('HTTP '+str(version)).encode(),parents=(current,)).to_raw())
        before=snapshot(repo.git_dir)
        class LegacyReport:
            def discover_push(self,control):
                ad=transport.discover_push(control)
                return replace(ad,capabilities=tuple(c for c in ad.capabilities if c.name!=b'report-status-v2'))
            send_pack=transport.send_pack
        selected=LegacyReport() if version==1 else transport
        result=repo.push(selected,(PushUpdate('refs/heads/main',next_oid,expected_old=current),))
        if not result.complete or result.report_version!=(1 if version==1 else 2):raise RuntimeError('native HTTP report version mismatch')
        if snapshot(repo.git_dir)!=before:raise RuntimeError('HTTP push mutated sender')
        if not fetched.fetch(transport).complete or fetched.refs.resolve('refs/remotes/origin/main').oid!=next_oid:
            raise RuntimeError('native HTTP fetch mismatch')
        if fetched.refs.resolve().oid!=current or fetched.status():raise RuntimeError('fetch moved sender worktree/HEAD')
        current=next_oid
    proposals=tuple(PushUpdate('refs/tags/atomic-'+str(n),current,expected_old=None) for n in range(2))
    result=repo.push(transport,proposals,atomic=True)
    if not result.complete or not result.atomic or not result.peer_atomic:raise RuntimeError('native advertised atomic push mismatch')
    deleted=tuple(PushUpdate(p.name,None,expected_old=current) for p in proposals)
    if not repo.push(transport,deleted,atomic=True).complete:raise RuntimeError('native atomic delete mismatch')
    # Bad incoming packs and wire-budget failures leave an existing destination unchanged.
    clean=Repository.init(root/'rejected',object_format=format)
    before=snapshot(clean.git_dir)
    for bad,limits in ((SmartHTTPTransport(spec['corrupt']+'/http-source.git',allow_http=True),None),
                       (LocalTransport(repo),TransferLimits(max_wire_bytes=8))):
        try:clean.fetch(bad,limits=limits)
        except GitdxError:pass
        else:raise RuntimeError('corruption/resource fixture accepted')
        if snapshot(clean.git_dir)!=before or not clean.refs.resolve().unborn:raise RuntimeError('rejected transfer mutated destination')
    return {'local':str(new),'http':str(current)}


def refusals(source,root):
    cases=('unknown-extension','unknown-format','reftable','alternates','shallow','promisor-config','promisor-marker')
    for name in cases:
        folder=root/name;shutil.copytree(source,folder);metadata=folder/'.git';config=metadata/'config'
        if name in ('unknown-extension','unknown-format','reftable'):
            git(folder,'config','core.repositoryformatversion','1')
            key,value={'unknown-extension':('unknown','required'),'unknown-format':('objectformat','unknown'),'reftable':('refstorage','reftable')}[name]
            with config.open('ab') as stream:stream.write(('[extensions]\n'+key+'='+value+'\n').encode())
        elif name=='alternates':(metadata/'objects/info/alternates').write_bytes(b'elsewhere\n')
        elif name=='shallow':(metadata/'shallow').write_bytes(b'unsupported\n')
        elif name=='promisor-config':
            with config.open('ab') as stream:stream.write(b'[remote "origin"]\npromisor=true\npartialclonefilter=blob:none\n')
        else:(metadata/'objects/pack/pack-qualification.promisor').write_bytes(b'')
        before=snapshot(folder)
        try:Repository.open(folder)
        except UnsupportedFormat:pass
        else:raise RuntimeError('unsupported profile accepted: '+name)
        if snapshot(folder)!=before:raise RuntimeError('refused profile was mutated: '+name)
    # Real native promisor metadata is also refused, without completing missing blobs.
    git(source,'config','uploadpack.allowFilter','true');partial=root/'native-promisor'
    git(root,'clone','--bare','--no-local','--filter=blob:none',str(source/'.git'),str(partial))
    before=snapshot(partial)
    try:Repository.open(partial)
    except UnsupportedFormat:pass
    else:raise RuntimeError('native partial clone accepted')
    if snapshot(partial)!=before:raise RuntimeError('native partial clone was mutated')


def index_and_history_refusals(source,root,format):
    for name in ('index-v3','index-v4','split-index','sparse-index','required-index','bad-index-checksum','grafts','replace'):
        folder=root/name;shutil.copytree(source,folder);metadata=folder/'.git';index=metadata/'index'
        if name=='index-v3':
            body=index.read_bytes()[:-format.digest_size]
            body=body[:4]+(3).to_bytes(4,'big')+body[8:]
            index.write_bytes(body+hashlib.new(format.value,body).digest())
            git(folder,'ls-files','--stage')
        elif name=='index-v4':git(folder,'update-index','--index-version=4')
        elif name=='split-index':git(folder,'update-index','--split-index')
        elif name=='sparse-index':
            git(folder,'sparse-checkout','init','--cone','--sparse-index');git(folder,'sparse-checkout','set','not-selected')
            try:Index.from_bytes(index.read_bytes(),format)
            except UnsupportedFormat:pass
            else:raise RuntimeError('native sparse index unexpectedly accepted')
        elif name=='required-index':
            body=index.read_bytes()[:-format.digest_size]+b'abcd'+(0).to_bytes(4,'big')
            index.write_bytes(body+hashlib.new(format.value,body).digest())
        elif name=='bad-index-checksum':
            data=index.read_bytes();index.write_bytes(data[:-1]+bytes((data[-1]^1,)))
        elif name=='grafts':(metadata/'info/grafts').write_bytes(b'')
        else:git(folder,'replace','HEAD','HEAD~1');git(folder,'pack-refs','--all')
        before=snapshot(folder)
        try:
            repo=Repository.open(folder)
            if name in ('grafts','replace'):repo.resolve('HEAD')
            else:repo.index.read()
        except (InvalidIndex if name=='bad-index-checksum' else UnsupportedFormat):pass
        else:raise RuntimeError('unsupported index/history profile accepted: '+name)
        if snapshot(folder)!=before:raise RuntimeError('refused index/history profile was mutated: '+name)


def run_worker(specfile):
    try:
        result=subprocess.run([sys.executable,'-I','-W','error::ResourceWarning',str(Path(__file__).resolve()),'--runtime',str(specfile)],
                              capture_output=True,env=dict(os.environ,PATH='',HTTP_PROXY='http://127.0.0.1:1',
                              HTTPS_PROXY='http://127.0.0.1:1',GIT_ASKPASS='must-not-execute'),check=True,timeout=180)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('no-Git runtime worker failed:\n'+error.stderr.decode('utf-8','replace')[-8192:]) from None
    return json.loads(result.stdout)


def inspect_native(spec):
    if os.environ.get('PATH')!='':raise RuntimeError('native inspection requires Git absent from PATH')
    for record in spec['inspect']:
        repo=Repository.open(record['root'])
        if str(repo.refs.resolve().oid)!=record['tip']:raise RuntimeError('reverse native ref mismatch')
        for hex,kind,digest in record['objects']:
            raw=repo.objects.read(ObjectId(hex,repo.object_format))
            if raw.kind.value!=kind or hashlib.sha256(raw.data).hexdigest()!=digest:raise RuntimeError('reverse native object mismatch')
    print(json.dumps({'native-inspection':True}))


def native_reverse(folder,url,new):
    receiver=folder/'local-receiver.git';git(receiver,'config','http.receivepack','true')
    git(receiver,'index-pack','--strict','--stdin',data=(folder/'gitdx-output.pack').read_bytes())
    records=[]
    for name,endpoint in (('local',str(receiver)),('http',url+'/local-receiver.git')):
        native=folder/('native-'+name)
        git(folder,'-c','protocol.version=2','clone','--no-local',endpoint,str(native))
        if name=='local' and git(native,'rev-parse','HEAD').strip().decode()!=new:raise RuntimeError('reverse native clone mismatch')
        (native/'native.bin').write_bytes(name.encode()+b'\0\xff\r\n');git(native,'add','native.bin');git(native,'commit','-m','native '+name)
        git(native,'push','origin','main');tip=git(native,'rev-parse','HEAD').strip().decode()
        git(receiver,'gc','--prune=now');git(receiver,'fsck','--full','--no-dangling')
        records.append({'root':str(receiver),'tip':tip,'objects':native_objects(receiver)})
        # Inspect before the next native client changes the same destination.
        specfile=folder/('inspect-'+name+'.json');specfile.write_text(json.dumps({'inspect':records[-1:]}))
        if not run_worker(specfile).get('native-inspection'):raise RuntimeError('native inspection did not complete')


def qualify(args):
    if not shutil.which('git') or not shutil.which('uv'):raise RuntimeError('Git and uv are mandatory qualification tools')
    if args.installed_prefix and not Path(gitdx.__file__).resolve().is_relative_to(Path(args.installed_prefix).resolve()):
        raise RuntimeError('qualification did not import the selected installation')
    if args.profile!='source' and not args.installed_prefix:raise RuntimeError('installed qualification requires its selected prefix')
    artifact=hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest() if args.artifact else None
    # Development-only CGI peer shared with the regression suite. Gitdx was
    # imported from the selected installation before adding the tests directory.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from test_http_transport import http_fixture
    import test_merge
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_merge))
    if not result.wasSuccessful() or result.skipped:raise RuntimeError('merge native/conflict/recovery qualification failed or skipped')
    import test_reset_restore
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_reset_restore))
    if not result.wasSuccessful() or result.skipped:raise RuntimeError('reset/restore native/recovery qualification failed or skipped')


    import test_receive
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_receive))
    if not result.wasSuccessful() or result.skipped:raise RuntimeError('receive qualification failed or skipped')
    import test_ref_transaction
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(test_ref_transaction))
    if not result.wasSuccessful() or result.skipped:raise RuntimeError('ref transaction qualification failed or skipped')
    import test_ingestion
    cases=(test_ingestion.IngestionTests,test_ingestion.IngestionNativeTests)
    if os.name=='posix':cases+=(test_ingestion.DescriptorStorageTests,)
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases)
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped:raise RuntimeError('ingestion qualification failed or skipped')
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for format in ObjectFormat:
            folder=root/format.value;folder.mkdir();spec=make_fixture(folder,format);source=Path(spec['root'])
            git(folder,'clone','--bare','--no-local',str(source/'.git'),str(folder/'http-source.git'))
            git(folder/'http-source.git','config','http.receivepack','true')
            with http_fixture(folder) as (url,_),http_fixture(folder,mode='corrupt-pack') as (corrupt,_):
                spec.update(http=url,corrupt=corrupt);specfile=folder/'runtime.json';specfile.write_text(json.dumps(spec))
                result=run_worker(specfile);new=result['new']
                if tuple(result['checks'])!=CHECKS:raise RuntimeError('incomplete runtime qualification')
                if git(folder/'http-source.git','rev-parse','HEAD').strip().decode()!=result['transfers']['http']:
                    raise RuntimeError('native HTTP push ref mismatch')
                git(folder/'http-source.git','fsck','--full','--no-dangling');native_reverse(folder,url,new)
            if git(source,'status','--porcelain') or git(source,'rev-parse','HEAD').strip().decode()!=new:raise RuntimeError('native post-write mismatch')
            git(source,'fsck','--full','--no-dangling');index_and_history_refusals(source,folder,format);refusals(source,folder)
    tools=dict(runtime_toolchain(),git=subprocess.run(['git','--version'],capture_output=True,text=True,check=True).stdout.strip(),
               uv=subprocess.run(['uv','--version'],capture_output=True,text=True,check=True).stdout.strip())
    selected=QualificationEvidence(implementation_digest(),args.profile,tools,CHECKS,CAPABILITIES,
                                   source_revision=args.source_revision,artifact_sha256=artifact)
    capability_report(selected)
    if args.output:
        destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text(selected.to_json())
    else:print(selected.to_json(),end='')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime');parser.add_argument('--profile',choices=('source','wheel','sdist'),default='source')
    parser.add_argument('--artifact');parser.add_argument('--installed-prefix');parser.add_argument('--source-revision');parser.add_argument('--output')
    args=parser.parse_args()
    if args.runtime:
        def forbidden(*args,**kwargs):raise RuntimeError('runtime attempted to execute an external helper')
        subprocess.run=forbidden
        subprocess.Popen=forbidden
        os.system=forbidden
        spec=json.loads(Path(args.runtime).read_text())
        if 'inspect' in spec:inspect_native(spec)
        else:runtime(spec)
    else:qualify(args)


if __name__=='__main__':main()
