"""
import_harvested.py — Upsert annotated harvest records into golden_set.json.

Run after human annotation of review_queue.jsonl:
    python import_harvested.py --input review_queue.jsonl           # dry-run (safe)
    python import_harvested.py --input review_queue.jsonl --apply   # write to golden_set.json

Only records with "include": true are imported.
Records with "include": null are reported as unannotated and block the import.
IDs are assigned as H-GS-001, H-GS-002, ... continuing from the last existing H-GS-NNN.

Validation checks before import:
  - dynamic=true  → expected_answer must be null, format_checks must be set
  - dynamic=false → expected_answer must not be null
  - eval_dimensions must be a non-empty list
  - expected_routing must be set
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import langfuse_client as lfc
from config import LANGFUSE_DATASET_NAME

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"

VALID_ROUTINGS   = {"docs_agent", "ui_agent", "api_agent", "reject", "direct_messaging_agent"}
VALID_DIMENSIONS = {"routing", "answered", "correctness", "guardrail", "link_valid", "format",
                    "must_include", "coherence"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_record(record: dict) -> list[str]:
    """Return list of validation errors for an annotated record."""
    errors: list[str] = []
    q = record.get("question", "")[:50]

    if record.get("expected_routing") not in VALID_ROUTINGS:
        errors.append(f"  [{q}] invalid expected_routing: {record.get('expected_routing')!r}")

    dims = record.get("eval_dimensions", [])
    if not dims:
        errors.append(f"  [{q}] eval_dimensions is empty")
    unknown_dims = set(dims) - VALID_DIMENSIONS
    if unknown_dims:
        errors.append(f"  [{q}] unknown eval_dimensions: {unknown_dims}")

    dynamic = record.get("dynamic")
    if dynamic is None:
        errors.append(f"  [{q}] dynamic is null — set to true or false")
    elif dynamic:
        if record.get("expected_answer") is not None:
            errors.append(
                f"  [{q}] dynamic=true but expected_answer is set — "
                "this answer will be stale tomorrow. Set expected_answer: null "
                "and use format_checks to describe the expected structure instead."
            )
        if not record.get("format_checks"):
            errors.append(
                f"  [{q}] dynamic=true but format_checks is null — "
                "add at least {{\"contains_number\": true}} so the scorer can "
                "verify the response has the right structure."
            )
    else:
        if not record.get("expected_answer"):
            errors.append(f"  [{q}] dynamic=false but expected_answer is null or empty")

    return errors


# ---------------------------------------------------------------------------
# ID assignment
# ---------------------------------------------------------------------------

def next_harvested_id(cases: list[dict]) -> str:
    existing = [
        int(c["id"].split("-")[-1])
        for c in cases
        if c["id"].startswith("H-GS-")
    ]
    n = max(existing, default=0) + 1
    return f"H-GS-{n:03d}"


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

def record_to_case(record: dict, case_id: str) -> dict:
    dynamic = bool(record.get("dynamic"))
    source  = record.get("_source", {})
    return {
        "id":               case_id,
        "suite":            "H",
        "harvested":        True,
        "source_trace_id":  source.get("trace_id"),
        "harvested_at":     source.get("timestamp"),
        "dynamic":          dynamic,
        "multi_turn":       False,
        "question":         record["question"],
        "expected_routing": record["expected_routing"],
        "expected_answer":  None if dynamic else record.get("expected_answer"),
        "must_include":     [],
        "must_not_include": record.get("must_not_include", []),
        "eval_dimensions":  record.get("eval_dimensions", []),
        "link_path":        None,
        "format_checks":    record.get("format_checks") if dynamic else None,
        "notes":            record.get("notes", ""),
        "known_status":     "new",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def import_harvested(input_path: Path, apply: bool = False) -> None:
    lines = [l for l in input_path.read_text().splitlines() if l.strip()]
    records = [json.loads(l) for l in lines]

    to_import   = [r for r in records if r.get("include") is True]
    skipped     = [r for r in records if r.get("include") is False]
    unannotated = [r for r in records if r.get("include") is None]

    print(f"Review queue: {len(records)} total")
    print(f"  include=true:  {len(to_import)}")
    print(f"  include=false: {len(skipped)}")
    print(f"  unannotated:   {len(unannotated)}")

    if unannotated:
        print(f"  (skipping {len(unannotated)} unannotated — annotate and re-run to pick them up)")

    if not to_import:
        print("\nNothing to import.")
        return

    # Validate all records before touching golden_set.json
    all_errors: list[str] = []
    for r in to_import:
        all_errors.extend(validate_record(r))

    if all_errors:
        print(f"\n⛔ {len(all_errors)} validation error(s) — fix before importing:\n")
        for e in all_errors:
            print(e)
        sys.exit(1)

    # Load golden set
    data  = json.loads(GOLDEN_SET_PATH.read_text())
    cases = data["cases"]
    existing_qs = {c.get("question", "").strip().lower() for c in cases}

    new_cases: list[dict] = []
    for record in to_import:
        q = record.get("question", "").strip().lower()
        if q in existing_qs:
            print(f"  SKIP (duplicate): {record['question'][:70]}")
            continue

        case_id  = next_harvested_id(cases + new_cases)
        new_case = record_to_case(record, case_id)
        new_cases.append(new_case)
        existing_qs.add(q)

        dyn_marker = " [dynamic]" if new_case["dynamic"] else ""
        print(f"  {'IMPORT' if apply else 'PREVIEW'} {case_id}{dyn_marker}: {record['question'][:65]}")

    if not new_cases:
        print("\nAll selected records were duplicates — nothing new to add.")
        return

    dynamic_count = sum(1 for c in new_cases if c["dynamic"])
    static_count  = len(new_cases) - dynamic_count
    print(f"\n{'Would add' if not apply else 'Adding'} {len(new_cases)} case(s): "
          f"{static_count} static, {dynamic_count} dynamic")

    if not apply:
        print("[dry-run] Pass --apply to write to golden_set.json")
        return

    # Write to golden_set.json
    cases.extend(new_cases)
    data["cases"] = cases
    data["_metadata"]["total_cases"] = len(cases)

    h_count = sum(1 for c in cases if c.get("suite") == "H")
    data["_metadata"]["suites"]["H"] = (
        f"Harvested from production traces ({h_count} cases — "
        f"usage-weighted, real user phrasing)"
    )

    GOLDEN_SET_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"\n✓ {len(new_cases)} case(s) written to {GOLDEN_SET_PATH}")
    print(f"  Total cases now: {len(cases)}")

    # Post to Langfuse dataset
    try:
        lf = lfc.get_client()
        for case in new_cases:
            if case["dynamic"]:
                expected = {
                    "expected_routing": case["expected_routing"],
                    "dynamic":          True,
                    "format_checks":    case.get("format_checks"),
                }
            else:
                expected = {
                    "expected_routing": case["expected_routing"],
                    "expected_answer":  case.get("expected_answer"),
                    "dynamic":          False,
                }
            lf.create_dataset_item(
                dataset_name=LANGFUSE_DATASET_NAME,
                input={"question": case["question"]},
                expected_output=expected,
                metadata={
                    "suite":            "H",
                    "harvested":        True,
                    "source_trace_id":  case.get("source_trace_id"),
                    "dynamic":          case["dynamic"],
                    "eval_dimensions":  case.get("eval_dimensions", []),
                    "must_not_include": case.get("must_not_include", []),
                    "format_checks":    case.get("format_checks"),
                    "notes":            case.get("notes", ""),
                    "known_status":     "new",
                },
                id=case["id"],
            )
        lf.flush()
        print("  ✓ Langfuse dataset updated")
    except Exception as e:
        print(f"  ⚠ Langfuse upsert failed: {e}")
        print("    golden_set.json was still updated successfully")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import annotated harvest records into golden_set.json"
    )
    parser.add_argument("--input", type=Path, required=True,
                        help="Annotated JSONL from harvest_traces.py")
    parser.add_argument("--apply", action="store_true",
                        help="Write to golden_set.json (default: dry-run)")
    args = parser.parse_args()
    import_harvested(input_path=args.input, apply=args.apply)
