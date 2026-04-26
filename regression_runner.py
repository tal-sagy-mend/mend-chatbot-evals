"""
Regression runner — runs the full golden set against the live bot and scores results.

For each golden case:
  1. Creates a fresh conversation UUID
  2. Calls ask_assistant via the MCP proxy
  3. Waits briefly for the Langfuse trace to appear
  4. Scores the response (rule-based + LLM judge)
  5. Posts scores to Langfuse

Multi-turn cases run all turns in sequence within the same conversation.

Prerequisites:
  - MCP proxy must be running: python3 ~/mend-mcp-proxy/proxy.py &
  - ANTHROPIC_API_KEY must be set in .env

Usage:
    python regression_runner.py               # run full golden set
    python regression_runner.py --suite D1    # run only one suite
    python regression_runner.py --dry-run     # score without posting to Langfuse
    python regression_runner.py --skip-llm    # rule-based scores only (faster)
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import anthropic

import langfuse_client as lfc
from bot_client import MendAuthClient, MendBotClient, create_conversation
from scorers import (
    run_all_scorers,
    score_coherence,
    score_routing,
    score_answered,
    score_guardrail,
)

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"
TRACE_WAIT_SECONDS = 8  # seconds to wait for Langfuse trace after calling bot


# ---------------------------------------------------------------------------
# Golden set loading
# ---------------------------------------------------------------------------

def load_golden_set(suite_filter: str | None = None) -> list[dict]:
    data = json.loads(GOLDEN_SET_PATH.read_text())
    cases = data["cases"]
    if suite_filter:
        cases = [c for c in cases if c["suite"] == suite_filter.upper()]
    return cases


# ---------------------------------------------------------------------------
# Trace lookup (find the Langfuse trace created by the bot call)
# ---------------------------------------------------------------------------

def find_trace_for_session(lf, session_id: str, retries: int = 5, wait: float = 3.0):
    """
    Poll Langfuse until a trace appears for the given session_id.
    Returns the most recent trace or None.
    """
    for attempt in range(retries):
        traces = lfc.fetch_traces_for_session(lf, session_id)
        if traces:
            # Return the most recent trace (highest timestamp)
            return sorted(traces, key=lambda t: t.timestamp, reverse=True)[0]
        if attempt < retries - 1:
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Run a single-turn golden case
# ---------------------------------------------------------------------------

def run_single_turn_case(
    case: dict,
    bot: MendBotClient,
    auth: MendAuthClient,
    lf,
    anth_client: anthropic.Anthropic | None,
    dry_run: bool = False,
    skip_llm: bool = False,
) -> dict:
    case_id = case["id"]
    question = case["question"]

    print(f"  {case_id}: {question[:70]}")

    # 1. Create conversation + call bot
    conv_uuid = create_conversation(auth)
    page_url = case.get("page_url")
    try:
        response = bot.ask(conv_uuid, question, page_url=page_url)
    except Exception as e:
        print(f"    ✗ Bot call failed: {e}")
        return {"case_id": case_id, "error": str(e), "scores": {}}

    print(f"    → response: {response[:80]}...")

    # 2. Wait for Langfuse trace
    time.sleep(TRACE_WAIT_SECONDS)
    trace = find_trace_for_session(lf, conv_uuid)
    steps = lfc.extract_steps(trace, lf) if trace else None

    # 3. Score
    if skip_llm:
        case_no_llm = {**case, "eval_dimensions": [
            d for d in case.get("eval_dimensions", [])
            if d not in ("correctness", "coherence")
        ]}
        scores = run_all_scorers(case_no_llm, response, steps, anthropic_client=None)
    else:
        scores = run_all_scorers(case, response, steps, anthropic_client=anth_client)

    # 4. Post to Langfuse
    if trace and not dry_run:
        lfc.post_scores(lf, trace.id, scores)
    elif not trace:
        print(f"    ⚠ No trace found in Langfuse for session {conv_uuid}")

    return {
        "case_id": case_id,
        "question": question,
        "response": response,
        "trace_id": trace.id if trace else None,
        "scores": {k: {"value": v, "comment": c} for k, (v, c) in scores.items()},
    }


# ---------------------------------------------------------------------------
# Run a multi-turn golden case
# ---------------------------------------------------------------------------

def run_multi_turn_case(
    case: dict,
    bot: MendBotClient,
    auth: MendAuthClient,
    lf,
    anth_client: anthropic.Anthropic | None,
    dry_run: bool = False,
    skip_llm: bool = False,
) -> dict:
    case_id = case["id"]
    turns_config = case["turns"]
    print(f"  {case_id} (multi-turn, {len(turns_config)} turns)")

    # Share one conversation UUID across all turns
    conv_uuid = create_conversation(auth)
    turn_responses: list[str] = []
    turn_results: list[dict] = []

    for turn_cfg in turns_config:
        turn_num = turn_cfg["turn"]
        question = turn_cfg["question"]
        print(f"    T{turn_num}: {question[:60]}")

        try:
            response = bot.ask(conv_uuid, question)
        except Exception as e:
            print(f"      ✗ Bot call failed: {e}")
            turn_responses.append("")
            continue

        print(f"      → {response[:60]}...")
        turn_responses.append(response)

        # Wait for trace
        time.sleep(TRACE_WAIT_SECONDS)
        traces = lfc.fetch_traces_for_session(lf, conv_uuid)
        # Get the trace matching this turn (most recent that hasn't been scored yet)
        trace = sorted(traces, key=lambda t: t.timestamp, reverse=True)[0] if traces else None
        steps = lfc.extract_steps(trace, lf) if trace else None

        scores: dict[str, tuple[float, str]] = {}
        dims = turn_cfg.get("eval_dimensions", [])

        if "routing" in dims:
            scores["routing"] = score_routing(steps, turn_cfg["expected_routing"])
        if "answered" in dims:
            scores["answered"] = score_answered(response)
        if "guardrail" in dims:
            scores["guardrail"] = score_guardrail(
                response, turn_cfg.get("must_not_include", [])
            )

        if not skip_llm and anth_client:
            if "correctness" in dims and turn_cfg.get("expected_answer"):
                from scorers import score_correctness
                scores["correctness"] = score_correctness(
                    question=question,
                    response=response,
                    expected_answer=turn_cfg["expected_answer"],
                    notes=turn_cfg.get("notes", ""),
                    client=anth_client,
                )

            if "coherence" in dims and turn_num > 1 and len(turn_responses) >= 2:
                scores["coherence"] = score_coherence(
                    t1_question=turns_config[0]["question"],
                    t1_response=turn_responses[0],
                    t2_question=question,
                    t2_response=response,
                    t2_notes=turn_cfg.get("notes", ""),
                    client=anth_client,
                )

        if trace and not dry_run and scores:
            lfc.post_scores(lf, trace.id, scores)

        turn_results.append({
            "turn": turn_num,
            "question": question,
            "response": response,
            "trace_id": trace.id if trace else None,
            "scores": {k: {"value": v, "comment": c} for k, (v, c) in scores.items()},
        })

    return {"case_id": case_id, "turns": turn_results}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_regression_report(results: list[dict], cases: list[dict]) -> None:
    print(f"\n{'=' * 60}")
    print("Regression run complete")
    print(f"{'=' * 60}")

    # Separate gap cases from the main aggregate
    gap_ids = {c["id"] for c in cases if c.get("gap_case")}
    main_results = [r for r in results if r.get("case_id") not in gap_ids]
    gap_results  = [r for r in results if r.get("case_id") in gap_ids]

    agg: dict[str, list[float]] = {}
    for r in main_results:
        if "turns" in r:
            for turn in r["turns"]:
                for dim, info in turn.get("scores", {}).items():
                    agg.setdefault(dim, []).append(info["value"])
        else:
            for dim, info in r.get("scores", {}).items():
                agg.setdefault(dim, []).append(info["value"])

    if agg:
        print(f"\nAverage scores by dimension (excluding {len(gap_ids)} gap case(s)):")
        for dim, vals in sorted(agg.items()):
            avg = sum(vals) / len(vals)
            bar = "█" * int(avg * 20)
            print(f"  {dim:<15} {avg:.2f}  {bar}")

    # Surface failures (main cases only)
    failures = []
    for r in main_results:
        if "error" in r:
            failures.append(f"  [{r['case_id']}] BOT ERROR: {r['error']}")
        elif "turns" not in r:
            bad = [d for d, i in r.get("scores", {}).items() if i["value"] < 0.5]
            if bad:
                failures.append(f"  [{r['case_id']}] {r.get('question','')[:60]} → {bad}")

    if failures:
        print(f"\n⚠ {len(failures)} case(s) with scores < 0.5:")
        for f in failures:
            print(f)

    # Gap cases — report separately so they don't distort the aggregate
    if gap_results:
        print(f"\n── Gap cases (excluded from aggregate — tracked for index fix) ──")
        for r in gap_results:
            if "error" in r:
                status = f"ERROR: {r['error']}"
            else:
                scores = r.get("scores", {})
                vals = [f"{d}={i['value']:.1f}" for d, i in scores.items()]
                status = ", ".join(vals) if vals else "no scores"
            print(f"  [{r['case_id']}] {r.get('question','')[:60]} → {status}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(suite_filter: str | None = None, dry_run: bool = False, skip_llm: bool = False) -> None:
    lf = lfc.get_client()
    anth_client = anthropic.Anthropic() if not skip_llm else None

    cases = load_golden_set(suite_filter)
    print(f"Running {len(cases)} golden cases "
          f"({'all suites' if not suite_filter else suite_filter})"
          f"{' [dry-run]' if dry_run else ''}"
          f"{' [no LLM judge]' if skip_llm else ''}")

    auth = MendAuthClient()
    results = []

    with MendBotClient() as bot:
        bot.initialize()

        for case in cases:
            try:
                if case.get("multi_turn"):
                    result = run_multi_turn_case(
                        case, bot, auth, lf, anth_client, dry_run, skip_llm
                    )
                else:
                    result = run_single_turn_case(
                        case, bot, auth, lf, anth_client, dry_run, skip_llm
                    )
                results.append(result)
            except Exception as e:
                print(f"  ✗ [{case['id']}] Unexpected error: {e}")
                results.append({"case_id": case["id"], "error": str(e), "scores": {}})

    if not dry_run:
        lf.flush()
    print_regression_report(results, cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mend Chatbot Regression Runner")
    parser.add_argument("--suite", help="Run only a specific suite (D1, N, G, E, A, M)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score without posting to Langfuse")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Skip LLM-as-judge (rule-based only, faster)")
    args = parser.parse_args()
    run(suite_filter=args.suite, dry_run=args.dry_run, skip_llm=args.skip_llm)
