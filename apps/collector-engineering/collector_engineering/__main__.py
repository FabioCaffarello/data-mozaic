"""Command-line entry point for local draft authoring and validation only."""

import argparse
import json
import sys
from pathlib import Path

from .playbooks import (
    build_draft,
    dump_yaml,
    validate_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector-engineering")
    subparsers = parser.add_subparsers(dest="command", required=True)
    draft_parser = subparsers.add_parser(
        "draft", help="create a YAML draft from a transcript"
    )
    draft_parser.add_argument("transcript", type=Path)
    draft_parser.add_argument("--output", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a YAML draft")
    validate_parser.add_argument("playbook", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "draft":
            transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
            candidate = build_draft(transcript)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(dump_yaml(candidate), encoding="utf-8")
            print(f"wrote draft {candidate['playbook_id']} to {args.output}")
            return 0
        if args.command == "validate":
            errors = validate_file(args.playbook)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("CapturePlaybook draft is schema and semantically valid")
            return 0
    except (OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
