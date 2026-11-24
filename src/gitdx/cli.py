"""Small argparse plumbing commands over the public repository API."""

import argparse
from pathlib import Path
import sys
import json
import os

from .errors import GitdxError
from .object_store import ObjectLimitExceeded, StoreLimits
from .objects import ObjectFormat, ObjectId, ObjectType, RawObject, parse_object
from .repository import Repository, RepositoryNotFound
from .push import PushUncertain, PushUpdate


def _push_json(result):
    return {'complete':result.complete,'atomic':result.atomic,'atomic_requested':result.atomic_requested,
            'peer_atomic':result.peer_atomic,'report_version':result.report_version,
            'outcomes':[{'name':o.name,'old':str(o.old) if o.old else None,'new':str(o.new) if o.new else None,
                         'state':o.state.value,'reason':o.reason} for o in result.outcomes]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Git-compatible repository operations in Python")
    parser.add_argument("--version", action="version", version="gitdx 0.1.0")
    parser.add_argument("-C", dest="directory", default=".", help="operate relative to this directory")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="create a new repository without replacing metadata")
    initialize.add_argument("path", nargs="?", default=".")
    initialize.add_argument("--bare", action="store_true")
    initialize.add_argument("--object-format", choices=list(ObjectFormat), default="sha1")
    initialize.add_argument("--initial-branch", default="main")
    hash_object = commands.add_parser("hash-object", help="hash bytes and optionally store a loose object")
    source = hash_object.add_mutually_exclusive_group(required=True)
    source.add_argument("path", nargs="?")
    source.add_argument("--stdin", action="store_true")
    hash_object.add_argument("-t", "--type", choices=list(ObjectType), default="blob")
    hash_object.add_argument("-w", "--write", action="store_true")
    hash_object.add_argument("--object-format", choices=list(ObjectFormat))
    cat = commands.add_parser("cat-file", help="read a loose object's raw payload or metadata")
    cat.add_argument("oid")
    detail = cat.add_mutually_exclusive_group()
    detail.add_argument("-t", "--type", action="store_true")
    detail.add_argument("-s", "--size", action="store_true")
    add = commands.add_parser("add", help="stage explicit files/directories and tracked removals")
    add.add_argument("paths",nargs="+")
    add.add_argument("-f","--force",action="store_true")
    status = commands.add_parser("status",help="show deterministic porcelain status")
    status.add_argument("--porcelain",action="store_true")
    status.add_argument("-z",action="store_true",help="NUL-terminate unquoted byte paths")
    commit = commands.add_parser("commit", help="commit the index with explicit identity and expected HEAD")
    commit.add_argument("--author", required=True)
    commit.add_argument("--committer", required=True)
    commit.add_argument("--expected-head", required=True, help="full object ID or unborn")
    commit.add_argument("--parent", action="append")
    commit.add_argument("-m", "--message", required=True)
    log = commands.add_parser("log", help="bounded parent-order history as JSON lines")
    log.add_argument("--start")
    log.add_argument("--max-count", type=int, default=100)
    commands.add_parser("branches", help="list local branches as JSON lines")
    commands.add_parser("tags", help="list tags as JSON lines")
    diff = commands.add_parser('diff', help='compare two explicit tree IDs; empty selects an empty tree')
    diff.add_argument('old_tree');diff.add_argument('new_tree')
    diff.add_argument('--context', type=int, default=3)
    diff.add_argument('--renames', type=int, metavar='PERCENT', help='enable bounded rename matching, threshold 1..100')
    diff.add_argument('--patch', action='store_true', help='print an escaped ASCII patch presentation')
    revision = commands.add_parser('rev-parse', help='resolve the documented single-object revision subset')
    revision.add_argument('revision')
    ancestor = commands.add_parser('is-ancestor', help='return status 0 for ancestry, 1 for false, 2 for errors')
    ancestor.add_argument('ancestor');ancestor.add_argument('descendant')
    bases = commands.add_parser('merge-base', help='print all best common ancestors of exactly two commit-ish roots')
    bases.add_argument('left');bases.add_argument('right')
    checkout = commands.add_parser("checkout",help="apply a safe checkout or print its reviewed changes")
    checkout.add_argument("destination")
    checkout.add_argument("--detach",action="store_true",help="destination is a full commit ID")
    checkout.add_argument("--unborn",action="store_true",help="explicitly select an absent branch with an empty tree")
    checkout.add_argument("--overwrite-tracked",action="store_true",help="discard tracked changes; untracked content stays protected")
    checkout.add_argument("--dry-run",action="store_true",help="print the immutable plan without mutation")
    commands.add_parser("recover-checkout",help="roll back an interrupted checkout or finalize a committed journal")
    capabilities = commands.add_parser('capabilities',help='print supported and explicitly qualified capability profiles')
    capabilities.add_argument('--evidence',help='selected qualification JSON for this exact installed runtime')
    clone = commands.add_parser('clone',help='clone through an explicitly selected transport into a new path')
    clone.add_argument('source');clone.add_argument('path');clone.add_argument('--bare',action='store_true')
    clone.add_argument('--branch');clone.add_argument('--no-checkout',action='store_true')
    clone.add_argument('--object-format',choices=list(ObjectFormat))
    fetch = commands.add_parser('fetch',help='fetch validated objects and update tracking refs')
    fetch.add_argument('source');fetch.add_argument('--remote',default='origin')
    fetch.add_argument('--branch',action='append');fetch.add_argument('--tags',action='store_true')
    fetch.add_argument('--allow-rewind',action='store_true')
    push = commands.add_parser('push',help='push an explicit expected-old branch/tag proposal')
    push.add_argument('source',help='explicit destination path or URL')
    push.add_argument('--ref',required=True);push.add_argument('--expected-old',required=True,help='full ID or absent')
    proposed=push.add_mutually_exclusive_group(required=True)
    proposed.add_argument('--new',help='full source object ID');proposed.add_argument('--delete',action='store_true')
    push.add_argument('--force',action='store_true');push.add_argument('--atomic',action='store_true')
    for command in (clone,fetch,push):
        command.add_argument('--transport',choices=('local','http'),required=True)
        command.add_argument('--allow-http',action='store_true',help='explicitly permit cleartext HTTP')
        command.add_argument('--protocol-version',type=int,choices=(0,1,2),default=2)
        command.add_argument('--authorization-env',help='explicit environment variable containing a Basic/Bearer header')
        command.add_argument('--timeout',type=float,default=30)
    args = parser.parse_args(argv)
    try:
        base = Path(args.directory).absolute()
        if not base.is_dir():
            raise NotADirectoryError("-C must name an existing directory")
        if args.command=='capabilities':
            from .capabilities import QualificationEvidence, capability_report
            evidence=None
            if args.evidence:
                with (base/args.evidence).open('rb') as stream:data=stream.read(1024*1024+1)
                evidence=QualificationEvidence.from_json(data)
            print(json.dumps(capability_report(evidence),sort_keys=True))
        elif args.command in ('is-ancestor', 'merge-base'):
            repo = Repository.discover(base)
            if args.command == 'is-ancestor':return 0 if repo.is_ancestor(args.ancestor,args.descendant) else 1
            selected = repo.merge_bases(args.left,args.right)
            for oid in selected:print(oid)
            return 0 if selected else 1
        elif args.command == 'rev-parse':
            print(Repository.discover(base).resolve(args.revision))
        elif args.command == 'diff':
            from .diff import render_patch
            repo = Repository.discover(base)
            old = None if args.old_tree == 'empty' else ObjectId(args.old_tree, repo.object_format)
            new = None if args.new_tree == 'empty' else ObjectId(args.new_tree, repo.object_format)
            for change in repo.diff(old, new, context=args.context, detect_renames=args.renames is not None,
                                    rename_threshold=args.renames if args.renames is not None else 50):
                if args.patch:sys.stdout.write(render_patch(change.patch()))
                else:
                    print(json.dumps({'status':change.status, 'old_path_hex':change.old_path.hex() if change.old_path is not None else None,
                                      'new_path_hex':change.new_path.hex() if change.new_path is not None else None,
                                      'old_mode':change.old_mode, 'new_mode':change.new_mode,
                                      'old_oid':str(change.old_oid) if change.old_oid else None,
                                      'new_oid':str(change.new_oid) if change.new_oid else None,
                                      'binary':change.binary, 'similarity':change.similarity,
                                      'hunks':[{'old_start':h.old_start,'old_count':h.old_count,'new_start':h.new_start,'new_count':h.new_count,
                                                'lines':[{'kind':line.kind,'hex':line.data.hex()} for line in h.lines]} for h in change.hunks]}))
        elif args.command == "init":
            repo = Repository.init(base / args.path, bare=args.bare, object_format=args.object_format,
                                   initial_branch=args.initial_branch)
            print(f"Initialized {'bare ' if repo.bare else ''}repository in {repo.git_dir}")
        elif args.command in ('clone','fetch','push'):
            from .http_transport import HTTPAuthorization, SmartHTTPTransport
            from .transfers import LocalTransport, TransferLimits
            limits=TransferLimits(max_seconds=args.timeout)
            if args.transport=='local':
                if args.authorization_env or args.allow_http:raise ValueError('HTTP options require the HTTP transport')
                transport=LocalTransport(base/args.source)
            else:
                authorization=None
                if args.authorization_env:
                    value=os.environ.get(args.authorization_env)
                    if value is None:raise ValueError('selected authorization environment value is missing')
                    authorization=HTTPAuthorization(value)
                transport=SmartHTTPTransport(args.source,authorization=authorization,allow_http=args.allow_http,protocol_version=args.protocol_version)
            if args.command=='clone':
                repo=Repository.clone(transport,base/args.path,bare=args.bare,branch=args.branch,checkout=not args.no_checkout,
                                      object_format=args.object_format,limits=limits)
                print(json.dumps({'path':str(repo.git_dir),'object_format':repo.object_format.value,'head':str(repo.refs.resolve().oid) if repo.refs.resolve().oid else None}))
            elif args.command=='fetch':
                result=Repository.discover(base).fetch(transport,remote=args.remote,branches=args.branch,tags=args.tags,
                                                      allow_rewind=args.allow_rewind,limits=limits)
                print(json.dumps({'complete':result.complete,'received_objects':result.received_objects,
                                  'updated':[r.name for r in result.updated],
                                  'rejected':[{'name':r.name,'reason':r.reason} for r in result.rejected]}))
                if not result.complete:return 2
            else:
                repo=Repository.discover(base)
                expected=None if args.expected_old=='absent' else ObjectId(args.expected_old,repo.object_format)
                proposed=None if args.delete else ObjectId(args.new,repo.object_format)
                result=repo.push(transport,(PushUpdate(args.ref,proposed,expected_old=expected,force=args.force),),atomic=args.atomic,limits=limits)
                print(json.dumps(_push_json(result)))
                if not result.complete:return 2
        elif args.command == "hash-object":
            repo = None
            try:
                repo = Repository.discover(base)
            except RepositoryNotFound:
                if args.write:
                    raise
            format = ObjectFormat(args.object_format) if args.object_format else repo.object_format if repo else ObjectFormat.SHA1
            if args.write and format != repo.object_format:
                raise ValueError("selected object format does not match the repository")
            maximum = StoreLimits().max_object_bytes
            if args.stdin:
                data = sys.stdin.buffer.read(maximum + 1)
            else:
                with (base / args.path).open("rb") as stream:
                    data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ObjectLimitExceeded("input exceeds the default object byte limit")
            raw = RawObject(args.type, data)
            parse_object(raw, format)
            oid = repo.objects.write(raw) if args.write else raw.oid(format)
            sys.stdout.buffer.write(oid.hex.encode("ascii") + b"\n")
        elif args.command in ("add","status"):
            repo = Repository.discover(base)
            if args.command=="add":
                selected = [(base / p).relative_to(repo.work_tree).as_posix() for p in args.paths]
                repo.add(*selected,force=args.force)
            else:
                for item in repo.status():
                    name = item.path if args.z else (os.fsdecode(item.path) if all(32<=b<127 and b not in (34,92) for b in item.path) else json.dumps(os.fsdecode(item.path))).encode("utf-8")
                    sys.stdout.buffer.write(item.xy.encode("ascii")+b" "+name+(b"\0" if args.z else b"\n"))
        elif args.command in ("checkout","recover-checkout"):
            repo = Repository.discover(base)
            if args.command=="checkout":
                destination=ObjectId(args.destination,repo.object_format) if args.detach else args.destination
                plan=repo.plan_checkout(destination,unborn=args.unborn,overwrite_tracked=args.overwrite_tracked)
                if args.dry_run:
                    print(json.dumps({"target":str(plan.target) if plan.target else None,"target_ref":plan.target_name,
                                      "overwrite_tracked":plan.overwrite_tracked,"changes":[{"path_hex":c.path.hex(),"before_mode":c.before_mode,"after_mode":c.after_mode,
                                      "before_oid":str(c.before_oid) if c.before_oid else None,"after_oid":str(c.after_oid) if c.after_oid else None} for c in plan.changes]}))
                    return 0
                result=repo.apply_checkout(plan)
            else:result=repo.recover_checkout()
            print(json.dumps({"target":str(result.target) if result.target else None,"rolled_back":result.rolled_back,"cleanup_pending":result.cleanup_pending}))
        elif args.command in ("commit", "log", "branches", "tags"):
            repo = Repository.discover(base)
            if args.command == "commit":
                expected = None if args.expected_head == "unborn" else ObjectId(args.expected_head, repo.object_format)
                parents = None if args.parent is None else tuple(ObjectId(p, repo.object_format) for p in args.parent)
                print(repo.commit(author=args.author.encode(), committer=args.committer.encode(), message=args.message.encode()+b"\n", expected_head=expected, parents=parents))
            elif args.command == "log":
                for entry in repo.log(ObjectId(args.start, repo.object_format) if args.start else None, max_count=args.max_count):
                    print(json.dumps({"oid": str(entry.oid), "tree": str(entry.commit.tree), "parents": [str(p) for p in entry.commit.parents], "message": entry.commit.message.decode("utf-8", "backslashreplace")}))
            else:
                for name, oid in repo.branches() if args.command == "branches" else repo.tags():
                    print(json.dumps({"name": name, "oid": str(oid) if oid else None}))
        else:
            repo = Repository.discover(base)
            raw = repo.objects.read(ObjectId(args.oid, repo.object_format))
            if args.type:
                sys.stdout.buffer.write(raw.kind.value.encode("ascii") + b"\n")
            elif args.size:
                sys.stdout.buffer.write(str(len(raw.data)).encode("ascii") + b"\n")
            else:
                sys.stdout.buffer.write(raw.data)
        return 0
    except PushUncertain as error:
        print(json.dumps(_push_json(error.result)))
        print(f'gitdx: {error}',file=sys.stderr)
        return 3
    except (GitdxError, OSError, ValueError) as error:
        print(f"gitdx: {error}", file=sys.stderr)
        return 2 if args.command in ('is-ancestor','merge-base') else 1
