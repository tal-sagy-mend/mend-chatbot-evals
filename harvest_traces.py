"""
harvest_traces.py — Pull production Langfuse traces and build a human review queue
for golden set expansion.

Normal usage (fetch + autofill, then you just approve/reject):
    python harvest_traces.py --autofill          # fills routing, dynamic, dims, answers
    python harvest_traces.py --autofill --skip-llm  # same but skip Claude (no docs answers)

Other options:
    python harvest_traces.py --days 14 --max 50
    python harvest_traces.py --output my_queue.jsonl

With --autofill, the script pre-populates every annotation field it can infer:
  - expected_routing   ← copied from detected routing
  - dynamic            ← inferred from question/response patterns
  - eval_dimensions    ← inferred from cluster
  - format_checks      ← inferred from question keywords (dynamic cases only)
  - must_not_include   ← standard retrieval-failure strings
  - expected_answer    ← LLM-generated for docs/greeting/edge (unless --skip-llm);
                          URL extracted from response for navigation cases;
                          null for dynamic cases (time-sensitive — never auto-fill)

After autofill, your annotation work per record is:
  1. Read the pre-filled fields — correct anything wrong
  2. Set include: true | false
  That's it.

The queue file is append-safe: re-running never overwrites existing annotations.

After annotating, import ready records (unannotated ones are skipped):
    python import_harvested.py --input review_queue.jsonl           # dry-run
    python import_harvested.py --input review_queue.jsonl --apply   # write to golden_set.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import langfuse_client as lfc
from config import JUDGE_MODEL

GOLDEN_SET_PATH         = Path(__file__).parent / "golden_set.json"
DEFAULT_OUTPUT          = Path(__file__).parent / "review_queue.jsonl"
DEFAULT_DAYS            = 7
DEFAULT_MAX             = 30
DEFAULT_MAX_PER_CLUSTER = 10

CLUSTER_ROUTING_MAP = {
    "docs_agent":             "docs",
    "ui_agent":               "navigation",
    "api_agent":              "data",
    "direct_messaging_agent": "greeting",
    "reject":                 "edge",
}

CLUSTER_KEYWORDS: dict[str, list[str]] = {
    "docs":       ["what is", "what are", "how do", "how to", "explain",
                   "difference between", "what does", "what's", "tell me about"],
    "navigation": ["take me", "show me", "go to", "navigate", "open",
                   "where is", "where can i find", "bring me"],
    "data":       ["how many", "count", "list", "which project", "most findings",
                   "breakdown", "total", "exploitability"],
    "greeting":   ["hello", "hi ", "good morning", "good afternoon", "good evening",
                   "bye", "thanks", "thank you", "i need help"],
    "edge":       ["delete", "remove", "email me", "you are now", "no restrictions",
                   "chatgpt", "competitor", "snyk"],
}

DYNAMIC_QUESTION_RE = re.compile(
    r"\b(how many|count|total|breakdown|most findings|right now|currently|"
    r"latest|last scan|recent|which project|which application|exploitab)\b",
    re.IGNORECASE,
)
DYNAMIC_RESPONSE_RE = re.compile(
    r"\b\d+\s+(critical|high|medium|low|open|finding)|finding[s]?\s*[:\-]\s*\d+|total[:\s]+\d+",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"]+|/[\w\-/.]+(?:\?[^\s<>\"]*)?")
PAGE_CONTEXT_RE = re.compile(
    r"^\[UI context: the user is currently on the page:[^\]]*\]\s*",
    re.IGNORECASE,
)

AUTOFILL_DOCS_PROMPT = (
    "You are an expert on the Mend platform (application security scanning tool). "
    "Write a concise factual expected answer for this chatbot evaluation question. "
    "2-4 sentences. Stick to verifiable facts about the Mend platform only.\n\n"
    "Question: {question}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(q: str) -> str:
    return re.sub(r"\s+", " ", q.lower().strip())


def strip_page_context(q: str) -> str:
    return PAGE_CONTEXT_RE.sub("", q).strip()


def detect_routing(steps: str | None) -> str | None:
    if not steps:
        return None
    s = steps.lower()
    for agent in CLUSTER_ROUTING_MAP:
        if agent in s:
            return agent
    return None


def cluster_question(question: str, routing: str | None) -> str:
    if routing in CLUSTER_ROUTING_MAP:
        return CLUSTER_ROUTING_MAP[routing]
    q = question.lower()
    for cluster, keywords in CLUSTER_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return cluster
    return "unknown"


def is_likely_dynamic(question: str, response: str) -> bool:
    if DYNAMIC_QUESTION_RE.search(question):
        return True
    if DYNAMIC_RESPONSE_RE.search(response):
        return True
    return False


def load_existing_normalized_questions(path: Path) -> set[str]:
    data = json.loads(path.read_text())
    seen: set[str] = set()
    for case in data["cases"]:
        if case.get("multi_turn"):
            for turn in case.get("turns", []):
                seen.add(normalize(strip_page_context(turn.get("question", ""))))
        else:
            seen.add(normalize(strip_page_context(case.get("question", ""))))
    return seen


# ---------------------------------------------------------------------------
# Autofill helpers
# ---------------------------------------------------------------------------

def _infer_eval_dimensions(cluster: str, dynamic: bool) -> list[str]:
    if cluster == "docs":
        return ["routing", "answered", "correctness"]
    if cluster == "navigation":
        return ["routing", "answered", "link_valid"]
    if cluster == "data":
        return ["routing", "answered", "format"] if dynamic else ["routing", "answered", "correctness"]
    if cluster == "greeting":
        return ["routing", "answered"]
    if cluster == "edge":
        return ["routing", "guardrail"]
    return ["routing", "answered"]


def _infer_format_checks(question: str) -> dict:
    q = question.lower()
    checks: dict[str, bool] = {"contains_number": True}
    if any(w in q for w in ["critical", "high", "medium", "low", "severity"]):
        checks["mentions_severity"] = True
    if any(w in q for w in ["sca", "sast", "container", "scan type", "engine"]):
        checks["mentions_scan_types"] = True
    if any(w in q for w in ["exploit", "reachab"]):
        checks["mentions_exploitability"] = True
    if any(w in q for w in ["project", "application", "which"]):
        checks["names_project"] = True
    return checks


def _infer_must_not_include(cluster: str) -> list[str]:
    # Edge/reject cases need custom must_not_include per question — leave empty for reviewer
    if cluster == "edge":
        return []
    return ["I could not retrieve", "I'm unable to retrieve", "I am unable to retrieve"]


def _extract_url_from_response(response: str) -> str | None:
    for match in URL_RE.findall(response):
        if len(match) > 8 and ("/" in match):
            return match
    return None


def _generate_docs_answer(question: str, client) -> str:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": AUTOFILL_DOCS_PROMPT.format(question=question)}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"[autofill error: {e}]"


def autofill(record: dict, anth_client=None) -> dict:
    """
    Pre-populate annotation fields. Never overwrites a field the human already set.
    Dynamic cases never get an expected_answer — the data is time-sensitive.
    """
    cluster      = record.get("cluster", "unknown")
    routing      = record.get("detected_routing")
    question     = record.get("question", "")
    response     = record.get("bot_response_preview", "")
    dynamic_hint = record.get("dynamic_hint", False)

    # expected_routing
    if not record.get("expected_routing") and routing:
        record["expected_routing"] = routing

    # dynamic
    if record.get("dynamic") is None:
        record["dynamic"] = dynamic_hint

    dynamic = record["dynamic"]

    # eval_dimensions
    if not record.get("eval_dimensions"):
        record["eval_dimensions"] = _infer_eval_dimensions(cluster, dynamic)

    # format_checks (dynamic only)
    if dynamic and not record.get("format_checks"):
        record["format_checks"] = _infer_format_checks(question)

    # must_not_include
    if not record.get("must_not_include"):
        record["must_not_include"] = _infer_must_not_include(cluster)

    # expected_answer — static cases only; never for dynamic
    if not dynamic and not record.get("expected_answer"):
        if cluster == "docs":
            if anth_client:
                record["expected_answer"] = _generate_docs_answer(question, anth_client)
                record["_answer_source"] = "llm-generated — verify before approving"
            else:
                record["_answer_source"] = "skipped (--skip-llm) — fill in manually"
        elif cluster == "navigation":
            url = _extract_url_from_response(response)
            if url:
                record["expected_answer"] = f"Navigation link to {url}"
                record["_answer_source"] = "extracted from bot response"
            else:
                record["_answer_source"] = "no URL found in response — fill in manually"
        elif cluster == "greeting":
            record["expected_answer"] = (
                "A helpful, friendly response acknowledging the user and offering assistance."
            )
            record["_answer_source"] = "inferred from cluster"
        elif cluster == "edge" and routing == "reject":
            record["expected_answer"] = (
                "Politely refuses the request, explaining it is outside the bot's scope."
            )
            record["_answer_source"] = "inferred from cluster"

    if dynamic:
        record["_answer_source"] = "dynamic — expected_answer intentionally null"

    return record


# ---------------------------------------------------------------------------
# Main harvest function
# ---------------------------------------------------------------------------

def harvest(
    days: int = DEFAULT_DAYS,
    max_candidates: int = DEFAULT_MAX,
    max_per_cluster: int = DEFAULT_MAX_PER_CLUSTER,
    output_path: Path = DEFAULT_OUTPUT,
    do_autofill: bool = False,
    skip_llm: bool = False,
) -> None:
    anth_client = None
    if do_autofill and not skip_llm:
        try:
            import anthropic
            anth_client = anthropic.Anthropic()
        except Exception as e:
            print(f"  ⚠ Could not init Anthropic client ({e}) — docs answers will be skipped")

    lf = lfc.get_client()
    print(f"Fetching traces from the last {days} day(s)...")
    traces = list(lfc.fetch_recent_traces(lf, hours_back=days * 24, page_size=100, max_pages=20))
    print(f"  {len(traces)} raw traces fetched")

    existing = load_existing_normalized_questions(GOLDEN_SET_PATH)
    print(f"  {len(existing)} questions already in golden set (will be skipped)")

    seen_in_batch: set[str] = set()
    cluster_counts: dict[str, int] = {}
    candidates: list[dict] = []
    skipped_existing = 0
    skipped_duplicate = 0

    for trace in traces:
        raw_q = lfc.extract_question(trace)
        if not raw_q or not raw_q.strip():
            continue

        question = strip_page_context(raw_q)
        norm     = normalize(question)

        if norm in existing:
            skipped_existing += 1
            continue
        if norm in seen_in_batch:
            skipped_duplicate += 1
            continue

        response = lfc.extract_response(trace)
        steps    = lfc.extract_steps(trace)
        routing  = detect_routing(steps)
        cluster  = cluster_question(question, routing)

        if cluster_counts.get(cluster, 0) >= max_per_cluster:
            continue

        dynamic_hint = is_likely_dynamic(question, response or "")
        seen_in_batch.add(norm)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

        record: dict = {
            "_source": {
                "trace_id":   trace.id,
                "session_id": getattr(trace, "session_id", None),
                "timestamp":  trace.timestamp.isoformat() if trace.timestamp else None,
            },
            "question":             question,
            "bot_response_preview": (response or "")[:400],
            "detected_routing":     routing,
            "cluster":              cluster,
            "dynamic_hint":         dynamic_hint,
            # annotation fields
            "include":          None,
            "expected_routing": None,
            "dynamic":          None,
            "expected_answer":  None,
            "format_checks":    None,
            "must_not_include": [],
            "eval_dimensions":  [],
            "notes":            "",
        }

        if do_autofill:
            record = autofill(record, anth_client)

        candidates.append(record)
        if len(candidates) >= max_candidates:
            break

    # Sort: dynamic_hint=True first (need more careful review) then by cluster
    candidates.sort(key=lambda r: (r["cluster"], not r["dynamic_hint"]))

    # Append to existing queue — never overwrite annotated records
    existing_candidates: list[dict] = []
    if output_path.exists():
        existing_candidates = [
            json.loads(l) for l in output_path.read_text().splitlines() if l.strip()
        ]
        existing_trace_ids = {
            r.get("_source", {}).get("trace_id") for r in existing_candidates
        }
        candidates = [
            c for c in candidates
            if c["_source"].get("trace_id") not in existing_trace_ids
        ]
        if existing_candidates:
            print(f"  Appending to existing queue ({len(existing_candidates)} existing records kept)")

    all_candidates = existing_candidates + candidates
    output_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in all_candidates) + "\n"
    )

    # Summary
    dynamic_count = sum(1 for c in candidates if c["dynamic_hint"])
    pending_total = sum(1 for c in all_candidates if c.get("include") is None)
    done_total    = sum(1 for c in all_candidates if c.get("include") is not None)

    print(f"\nNew candidates added: {len(candidates)}")
    print(f"  Skipped (already in golden set): {skipped_existing}")
    print(f"  Skipped (duplicate in batch):    {skipped_duplicate}")
    if do_autofill:
        llm_count = sum(
            1 for c in candidates
            if c.get("_answer_source", "").startswith("llm")
        )
        print(f"  Answers auto-filled (LLM):       {llm_count}")
        print(f"  Dynamic (answer left null):       {dynamic_count}")
    if candidates:
        print("\nNew candidates by cluster:")
        for cluster in sorted(cluster_counts):
            n   = cluster_counts[cluster]
            dyn = sum(1 for c in candidates if c["cluster"] == cluster and c["dynamic_hint"])
            print(f"  {cluster:<15} {n:>3}  ({dyn} dynamic)")

    print(f"\nQueue status: {len(all_candidates)} total  |  "
          f"{done_total} annotated  |  {pending_total} pending")

    if do_autofill and pending_total:
        print(f"\nFor each pending record:")
        print(f"  1. Spot-check the pre-filled fields")
        print(f"  2. Set include: true or false")
    print(f"\nThen run:  python import_harvested.py --input {output_path} [--apply]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Harvest Langfuse traces into a review queue")
    parser.add_argument("--days",            type=int,  default=DEFAULT_DAYS,
                        help=f"Days back to fetch (default: {DEFAULT_DAYS})")
    parser.add_argument("--max",             type=int,  default=DEFAULT_MAX,
                        help=f"Max total new candidates (default: {DEFAULT_MAX})")
    parser.add_argument("--max-per-cluster", type=int,  default=DEFAULT_MAX_PER_CLUSTER,
                        help=f"Max per intent cluster (default: {DEFAULT_MAX_PER_CLUSTER})")
    parser.add_argument("--output",          type=Path, default=DEFAULT_OUTPUT,
                        help=f"Output path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--autofill",        action="store_true",
                        help="Pre-populate routing, dynamic, eval_dims, format_checks, answers")
    parser.add_argument("--skip-llm",        action="store_true",
                        help="With --autofill: skip LLM-generated docs answers (faster, no API cost)")
    args = parser.parse_args()
    harvest(
        days=args.days,
        max_candidates=args.max,
        max_per_cluster=args.max_per_cluster,
        output_path=args.output,
        do_autofill=args.autofill,
        skip_llm=args.skip_llm,
    )
