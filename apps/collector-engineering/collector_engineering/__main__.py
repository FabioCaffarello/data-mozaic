"""Command-line entry point for local draft validation and fixture replay."""

import argparse
import json
import sys
from pathlib import Path

from .playbooks import (
    PlaybookError,
    build_draft,
    dump_yaml,
    load_playbook,
    replay,
    validate_file,
)


def _write_report(path: Path | None, payload: dict) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(rendered, end="")
    else:
        path.write_text(rendered, encoding="utf-8")


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
    replay_parser = subparsers.add_parser(
        "replay", help="replay against captured fixtures only"
    )
    replay_parser.add_argument("playbook", type=Path)
    replay_parser.add_argument("--fixtures", type=Path, required=True)
    replay_parser.add_argument("--report", type=Path)
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
        report = replay(load_playbook(args.playbook), args.fixtures)
        _write_report(args.report, report)
        return 0 if report["state"] == "passed" else 1
    except (OSError, json.JSONDecodeError, PlaybookError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
