"""
Production eval runner — scores existing Langfuse traces.

Fetches traces from the last N hours (default 24h), matches them against the
golden set where possible, runs the applicable scorers, and posts scores back
to Langfuse. Designed to run on a nightly schedule.

Usage:
    python eval_runner.py              # score last 24h of traces
    python eval_runner.py --hours 48   # score last 48h
    python eval_runner.py --dry-run    # print scores without posting to Langfuse
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anthropic

import langfuse_client as lfc
from scorers import run_all_scorers, score_routing, score_answered, score_guardrail
from config import LANGFUSE_USER_ID

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"


# ---------------------------------------------------------------------------
# Golden set loader & matcher
# ---------------------------------------------------------------------------

def load_golden_set() -> list[dict]:
    data = json.loads(GOLDEN_SET_PATH.read_text())
    cases = []
    for case in data["cases"]:
        if case.get("multi_turn"):
            continue  # multi-turn handled by regression_runner only
        cases.append(case)
    return cases


def build_question_lookup(cases: list[dict]) -> dict[str, dict]:
    """Map normalized question string → golden case dict."""
    return {c["question"].strip().lower(): c for c in cases}


def match_trace_to_golden(question: str, lookup: dict[str, dict]) -> dict | None:
    """Try to match a trace question to a golden set case (exact, case-insensitive)."""
    return lookup.get(question.strip().lower())


# ---------------------------------------------------------------------------
# Scoring a single trace
# ---------------------------------------------------------------------------

def score_trace(
    trace,
    golden_lookup: dict[str, dict],
    anthropic_client: anthropic.Anthropic,
    dry_run: bool = False,
    lf=None,
) -> dict:
    question = lfc.extract_question(trace)
    response = lfc.extract_response(trace)
    steps = lfc.extract_steps(trace)

    golden_case = match_trace_to_golden(question, golden_lookup)
    scores: dict[str, tuple[float, str]] = {}

    if golden_case:
        scores = run_all_scorers(
            case=golden_case,
            response_text=response,
            steps=steps,
            anthropic_client=anthropic_client,
        )
    else:
        # Unmatched trace — run only what we can without expected answers
        if steps or response:
            scores["answered"] = score_answered(response)
        # Can't score routing without knowing the expected routing

    result = {
        "trace_id": trace.id,
        "question": question[:100],
        "session_id": trace.session_id,
        "matched_case": golden_case["id"] if golden_case else None,
        "scores": {k: {"value": v, "comment": c} for k, (v, c) in scores.items()},
    }

    if not dry_run and lf and scores:
        lfc.post_scores(lf, trace.id, scores)

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(results: list[dict]) -> None:
    total = len(results)
    matched = sum(1 for r in results if r["matched_case"])
    print(f"\n{'=' * 60}")
    print(f"Eval run complete — {total} traces scored, {matched} matched to golden set")
    print(f"{'=' * 60}")

    # Aggregate scores by dimension
    agg: dict[str, list[float]] = {}
    for r in results:
        for dim, info in r["scores"].items():
            agg.setdefault(dim, []).append(info["value"])

    if agg:
        print("\nAverage scores by dimension:")
        for dim, vals in sorted(agg.items()):
            avg = sum(vals) / len(vals)
            bar = "█" * int(avg * 20)
            print(f"  {dim:<15} {avg:.2f}  {bar}")

    # Surface failing traces
    failing = [
        r for r in results
        if any(info["value"] < 0.5 for info in r["scores"].values())
    ]
    if failing:
        print(f"\n⚠ {len(failing)} trace(s) with at least one score < 0.5:")
        for r in failing[:10]:
            bad_dims = [d for d, i in r["scores"].items() if i["value"] < 0.5]
            print(f"  [{r['matched_case'] or 'unmatched'}] {r['question'][:70]}")
            print(f"    failing dims: {bad_dims}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(hours_back: int = 24, dry_run: bool = False) -> list[dict]:
    lf = lfc.get_client()
    golden_cases = load_golden_set()
    golden_lookup = build_question_lookup(golden_cases)
    anth_client = anthropic.Anthropic()

    print(f"Fetching traces for user_id={LANGFUSE_USER_ID} from last {hours_back}h...")
    traces = list(lfc.fetch_recent_traces(lf, hours_back=hours_back))
    print(f"Found {len(traces)} traces")

    results = []
    for i, trace in enumerate(traces, 1):
        print(f"  [{i}/{len(traces)}] {lfc.extract_question(trace)[:80]}", end="  ")
        result = score_trace(
            trace,
            golden_lookup,
            anth_client,
            dry_run=dry_run,
            lf=lf,
        )
        dims = list(result["scores"].keys())
        print(f"→ scored: {dims}")
        results.append(result)

    if not dry_run:
        lf.flush()
        print(f"\nScores posted to Langfuse ({LANGFUSE_USER_ID})")
    else:
        print("\n[dry-run] Scores NOT posted to Langfuse")

    print_report(results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mend Chatbot Production Eval Runner")
    parser.add_argument("--hours", type=int, default=24,
                        help="How many hours back to fetch traces (default 24)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scores without posting to Langfuse")
    args = parser.parse_args()
    run(hours_back=args.hours, dry_run=args.dry_run)
