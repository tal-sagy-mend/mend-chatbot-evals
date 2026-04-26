"""
One-time script to import the golden set into Langfuse as a Dataset.

Run this once after reviewing and approving golden_set.json:
    python import_dataset.py
    python import_dataset.py --dry-run   # preview without importing

The dataset is named by LANGFUSE_DATASET_NAME in config.py.
Re-running is safe — items are upserted by their 'id' field if supported,
otherwise duplicates are appended (Langfuse doesn't deduplicate items).
To reset: delete the dataset in the Langfuse UI, then re-run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import langfuse_client as lfc
from config import LANGFUSE_DATASET_NAME, LANGFUSE_HOST

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"


def import_golden_set(dry_run: bool = False) -> None:
    data = json.loads(GOLDEN_SET_PATH.read_text())
    cases = data["cases"]
    meta = data.get("_metadata", {})

    print(f"Golden set: {meta.get('version', 'unknown')} — {len(cases)} cases")
    print(f"Target dataset: {LANGFUSE_DATASET_NAME}")
    if dry_run:
        print("[dry-run] No changes will be made")
        for case in cases:
            print(f"  {case['id']} [{case['suite']}] {'(dynamic)' if case.get('dynamic') else ''} "
                  f"{'(multi-turn)' if case.get('multi_turn') else ''} "
                  f"known_status={case.get('known_status', '?')}")
        return

    lf = lfc.get_client()

    # Create dataset — retry up to 3 times, abort if dataset can't be confirmed
    dataset_ready = False
    last_err = None
    for attempt in range(1, 4):
        try:
            lf.create_dataset(
                name=LANGFUSE_DATASET_NAME,
                description=(
                    "Mend Platform Chatbot golden evaluation set — 46 curated cases across "
                    "D1/N/G/E/A/M suites. Static cases include expected answers for LLM-judge scoring. "
                    "Dynamic cases (A suite) use format-only scoring."
                ),
            )
            print(f"Dataset '{LANGFUSE_DATASET_NAME}' created")
            dataset_ready = True
            break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            # Already exists is fine — proceed
            if "already exist" in err_str or "conflict" in err_str or "409" in err_str:
                print(f"Dataset '{LANGFUSE_DATASET_NAME}' already exists, continuing...")
                dataset_ready = True
                break
            print(f"  [attempt {attempt}/3] create_dataset failed: {e}")

    if not dataset_ready:
        print(f"\nAbort — could not create/verify dataset after 3 attempts: {last_err}")
        print("Check VPN connectivity to langfuse.mendinfra.com and retry.")
        return

    imported = 0
    errors = 0

    for case in cases:
        case_id = case["id"]

        # Build input
        if case.get("multi_turn"):
            inp = {
                "turns": [
                    {"turn": t["turn"], "question": t["question"]}
                    for t in case["turns"]
                ]
            }
        else:
            inp = {"question": case["question"]}

        # Build expected output
        if case.get("multi_turn"):
            expected = {
                "turns": [
                    {
                        "turn": t["turn"],
                        "expected_routing": t.get("expected_routing"),
                        "expected_answer": t.get("expected_answer"),
                    }
                    for t in case["turns"]
                ]
            }
        elif case.get("dynamic"):
            expected = {
                "expected_routing": case["expected_routing"],
                "dynamic": True,
                "format_checks": case.get("format_checks"),
            }
        else:
            expected = {
                "expected_routing": case["expected_routing"],
                "expected_answer": case.get("expected_answer"),
                "link_path": case.get("link_path"),
            }

        # Metadata carries everything needed by the scorers
        metadata = {
            "suite": case["suite"],
            "dynamic": case.get("dynamic", False),
            "multi_turn": case.get("multi_turn", False),
            "eval_dimensions": case.get("eval_dimensions", []),
            "must_include": case.get("must_include", []),
            "must_not_include": case.get("must_not_include", []),
            "format_checks": case.get("format_checks"),
            "notes": case.get("notes", ""),
            "known_status": case.get("known_status", "unknown"),
        }
        if case.get("multi_turn"):
            metadata["turns"] = case["turns"]

        try:
            lf.create_dataset_item(
                dataset_name=LANGFUSE_DATASET_NAME,
                input=inp,
                expected_output=expected,
                metadata=metadata,
                id=case_id,  # use case ID as stable item ID
            )
            print(f"  ✓ {case_id}")
            imported += 1
        except Exception as e:
            print(f"  ✗ {case_id}: {e}")
            errors += 1

    lf.flush()
    print(f"\nDone — {imported} items imported, {errors} errors")
    print(f"View at: {LANGFUSE_HOST}/datasets/{LANGFUSE_DATASET_NAME}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import golden set to Langfuse dataset")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making changes")
    args = parser.parse_args()
    import_golden_set(dry_run=args.dry_run)
