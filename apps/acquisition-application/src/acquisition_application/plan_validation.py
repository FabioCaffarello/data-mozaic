"""Re-evaluate result records using the exact declarative BC15 plan."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from collector_engineering import reference_runtime

from .result_bundle import FixtureImportError, VerifiedFixturePage


def verify_plan_results(
    *,
    plan: dict[str, Any],
    pages: tuple[VerifiedFixturePage, ...],
    records: tuple[dict[str, Any], ...],
    source_id: str,
    source_revision: str,
) -> None:
    """Recompute records from trusted page bytes and the build's typed plan.

    The plan must come from the exact BotBuild already selected and verified by
    the worker/catalog composition. This function deliberately has no fixture
    expected-record oracle or source-specific mapping.
    """
    if not isinstance(plan, dict) or plan.get("profile") not in {
        "scrapy-fixture-v1",
        "scrapy-live-http-v1",
    }:
        raise FixtureImportError("compiled acquisition plan profile is unsupported")
    source = plan.get("source")
    if (
        not isinstance(source, dict)
        or source.get("source_id") != source_id
        or source.get("revision") != source_revision
    ):
        raise FixtureImportError("compiled plan differs from trusted source binding")
    select = plan.get("select")
    extract = plan.get("extract")
    budgets = plan.get("budgets")
    if (
        not isinstance(select, dict)
        or not isinstance(extract, dict)
        or not isinstance(budgets, dict)
        or not isinstance(select.get("inputs"), dict)
        or not isinstance(extract.get("inputs"), dict)
    ):
        raise FixtureImportError("compiled plan operators are malformed")
    selection = select["inputs"]
    fields = extract["inputs"].get("fields")
    if not isinstance(fields, dict) or not fields:
        raise FixtureImportError("compiled extraction fields are invalid")
    try:
        maximum_records = int(budgets["max_records"])
        maximum_output = int(budgets["max_output_bytes"])
        maximum_pages = int(budgets["max_pages"])
    except (KeyError, TypeError, ValueError) as error:
        raise FixtureImportError("compiled plan budgets are invalid") from error
    if (
        maximum_records < 1
        or maximum_output < 1
        or maximum_pages < 1
        or len(pages) > maximum_pages
        or len(records) > maximum_records
    ):
        raise FixtureImportError("result exceeds compiled plan budgets")

    try:
        with tempfile.TemporaryDirectory(prefix="dm-acquisition-plan-") as root:
            real_root = Path(root).resolve(strict=True)
            fixture_root = real_root / "fixtures"
            output_root = real_root / "output"
            fixture_root.mkdir(mode=0o700)
            context = reference_runtime.RuntimeContext.create(
                str(fixture_root),
                str(output_root),
                source,
                budgets,
                [],
            )
            derived: list[dict[str, Any]] = []
            ordered_pages = sorted(
                pages,
                key=lambda page: next(
                    (
                        record.get("_provenance", {}).get("page_index", 0)
                        for record in records
                        if isinstance(record.get("_provenance"), dict)
                        and record["_provenance"].get("evidence_ref")
                        == page.evidence_ref
                    ),
                    0,
                ),
            )
            for page_index, page in enumerate(ordered_pages):
                document = {
                    "url": page.url,
                    "requested_url": page.url,
                    "body": page.body,
                    "sha256": page.digest.removeprefix("sha256:"),
                    "evidence_ref": page.evidence_ref,
                    "content_type": page.media_type,
                    "status": 200,
                    "page_index": page_index,
                }
                operator = select.get("operator_type")
                if operator == "select.html_records":
                    selected = reference_runtime.select_html_records(
                        context,
                        document,
                        selector=selection["selector"],
                        cardinality=selection.get("cardinality", "many"),
                        max_matches=selection["max_matches"],
                        evidence_refs=[page.evidence_ref],
                    )
                elif operator == "select.json_records":
                    selected = reference_runtime.select_json_records(
                        context,
                        document,
                        array_path=selection["array_path"],
                        max_matches=selection["max_matches"],
                        evidence_refs=[page.evidence_ref],
                    )
                else:
                    raise FixtureImportError(
                        "compiled selection operator is unsupported"
                    )
                derived.extend(
                    reference_runtime.extract_fields(
                        context,
                        selected,
                        fields=fields,
                        operation_id=extract.get("operation_id", "extract"),
                    )
                )
    except FixtureImportError:
        raise
    except Exception as error:  # runtime failures are untrusted plan/result errors
        raise FixtureImportError(
            "result cannot be reproduced from its compiled plan"
        ) from error

    actual = sorted(
        records,
        key=lambda record: (
            record.get("_provenance", {}).get("page_index", -1),
            record.get("_provenance", {}).get("record_index", -1),
        ),
    )
    expected = sorted(
        derived,
        key=lambda record: (
            record["_provenance"]["page_index"],
            record["_provenance"]["record_index"],
        ),
    )
    if actual != expected:
        raise FixtureImportError(
            "worker records do not match the compiled plan and trusted evidence"
        )
