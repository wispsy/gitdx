"""Small argparse plumbing commands over the public repository API."""

import argparse
from pathlib import Path
import sys

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
            print(repo.objects.write(raw) if args.write else raw.oid(format))
        else:
            repo = Repository.discover(base)
            raw = repo.objects.read(ObjectId(args.oid, repo.object_format))
            if args.type:
                print(raw.kind.value)
            elif args.size:
                print(len(raw.data))
            else:
                sys.stdout.buffer.write(raw.data)
        return 0
    except (GitdxError, OSError, ValueError) as error:
        print(f"gitdx: {error}", file=sys.stderr)
        return 1
