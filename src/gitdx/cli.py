"""Command-line entry point; repository plumbing is added separately."""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Git-compatible repository operations in Python")
    parser.add_argument("--version", action="version", version="gitdx 0.1.0")
    parser.parse_args(argv)
    return 0
