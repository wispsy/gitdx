"""Mandatory native Git qualification, plus a separate no-Git runtime worker."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import gitdx
from gitdx import (Blob, DirectRef, ObjectFormat, ObjectId, PackFile, PackIndex, QualificationEvidence,
                   RefConflict, Repository, UnsupportedFormat, capability_report, implementation_digest, runtime_toolchain)

IDENTITY=b'Qualification <qualification@example.invalid> 1700000000 +0000'
CHECKS=('native-objects','packed-read','delta-ofs','delta-ref','packed-refs','ref-cas',
        'write-after-gc','native-status','checkout','refusal-profiles','no-git-runtime')
CAPABILITIES=('objects.native','storage.loose','storage.pack-index-v2','storage.delta-ofs-ref',
              'refs.files-packed','worktree.index-stage-status','worktree.checkout','history.commit','config.local')


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
    return {'root':str(source),'format':format.value,'tip':tip,'old':old,'objects':objects,'indexed':indexed}


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
    print(json.dumps({'new':str(new),'checks':CHECKS[:-2]+('no-git-runtime',)}))


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


def qualify(args):
    if not shutil.which('git') or not shutil.which('uv'):raise RuntimeError('Git and uv are mandatory qualification tools')
    if args.installed_prefix and not Path(gitdx.__file__).resolve().is_relative_to(Path(args.installed_prefix).resolve()):
        raise RuntimeError('qualification did not import the selected installation')
    if args.profile!='source' and not args.installed_prefix:raise RuntimeError('installed qualification requires its selected prefix')
    artifact=hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest() if args.artifact else None
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for format in ObjectFormat:
            folder=root/format.value;folder.mkdir();spec=make_fixture(folder,format);specfile=folder/'runtime.json';specfile.write_text(json.dumps(spec))
            result=subprocess.run([sys.executable,'-I','-W','error::ResourceWarning',str(Path(__file__).resolve()),'--runtime',str(specfile)],
                                  capture_output=True,env=dict(os.environ,PATH=''),check=True)
            new=json.loads(result.stdout)['new'];source=Path(spec['root'])
            if git(source,'status','--porcelain') or git(source,'rev-parse','HEAD').strip().decode()!=new:raise RuntimeError('native post-write mismatch')
            git(source,'fsck','--full','--no-dangling');refusals(source,folder)
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
    if args.runtime:runtime(json.loads(Path(args.runtime).read_text()))
    else:qualify(args)


if __name__=='__main__':main()
