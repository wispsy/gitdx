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
    args = parser.parse_args(argv)
    try:
        base = Path(args.directory).absolute()
        if not base.is_dir():
            raise NotADirectoryError("-C must name an existing directory")
        if args.command == "init":
            repo = Repository.init(base / args.path, bare=args.bare, object_format=args.object_format,
                                   initial_branch=args.initial_branch)
            print(f"Initialized {'bare ' if repo.bare else ''}repository in {repo.git_dir}")
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
    except (GitdxError, OSError, ValueError) as error:
        print(f"gitdx: {error}", file=sys.stderr)
        return 1
