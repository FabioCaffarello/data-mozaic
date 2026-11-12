"""Validate Conventional Commit headers used by commits and pull request titles."""

import argparse
import re
import sys
from pathlib import Path

HEADER = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"\([a-z0-9]+(?:[/-][a-z0-9]+)*\)!?: .{1,72}$"
)


def validate(header: str) -> str | None:
    first_line = header.splitlines()[0].strip() if header.splitlines() else ""
    if not first_line:
        return "commit message header is empty"
    if len(first_line) > 100:
        return "commit message header exceeds 100 characters"
    if not HEADER.fullmatch(first_line):
        return (
            "expected '<type>(<scope>): <description>' using a Conventional "
            "Commit type and explicit lowercase scope"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="commit-msg hook input file")
    source.add_argument("--message", help="commit or pull request title")
    args = parser.parse_args()
    value = args.file.read_text() if args.file else args.message
    error = validate(value)
    if error:
        print(f"commit title check failed: {error}", file=sys.stderr)
        return 1
    print("commit title check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
