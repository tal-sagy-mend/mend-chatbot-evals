"""
Scorer functions for the Mend Platform Chatbot eval framework.

Each scorer returns (score: float, comment: str).
  - Routing, answered, link, guardrail, format: rule-based (deterministic)
  - Correctness, coherence: LLM-as-judge via Claude Sonnet with prompt caching
"""
from __future__ import annotations

import re
import json
import anthropic
from config import JUDGE_MODEL

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RETRIEVAL_FAILURE_PATTERNS = [
    "i could not retrieve",
    "i'm unable to retrieve",
    "i am unable to retrieve",
    "i don't have access to",
    "i do not have access to",
    "unable to provide that information",
    "i cannot access",
    "i can't access that",
    "information is not available",
    "not available in my knowledge",
    "i encountered an error processing",
    "i couldn't find a complete answer",
    "i could not find a complete answer",
]

# Pairs where the wrong-but-close routing scores 0.5 instead of 0.0
NEAR_MISS_PAIRS = {
    ("ui_agent", "api_agent"),
    ("api_agent", "ui_agent"),
}

JUDGE_SYSTEM_PROMPT = """You are an expert evaluator for the Mend Platform security chatbot.
Assess whether the bot's response is factually correct and complete based on the expected answer.

Scoring rubric:
- 1.0 — All key facts from the expected answer are present and correct
- 0.8 — Mostly correct; covers the main points with minor omissions
- 0.5 — Partially correct; gets some facts right but misses important elements
- 0.2 — Significant errors or major omissions
- 0.0 — Completely wrong, refuses to answer, or says "I could not retrieve"

Respond ONLY with valid JSON — no markdown, no explanation outside the JSON:
{"score": <float 0.0–1.0>, "reasoning": "<one concise sentence>"}"""

COHERENCE_SYSTEM_PROMPT = """You are evaluating whether a chatbot correctly maintains context across a multi-turn conversation.

Given Turn 1 and Turn 2, assess whether Turn 2 correctly references and builds on Turn 1's content.

Scoring rubric:
- 1.0 — Turn 2 perfectly builds on T1 context with accurate recall
- 0.7 — Turn 2 references T1 but with minor gaps or imprecision
- 0.3 — Turn 2 partially relates to T1 but misses key context or drifts
- 0.0 — Turn 2 ignores T1 entirely, hallucinates different content, or fails to answer

Respond ONLY with valid JSON — no markdown, no explanation outside the JSON:
{"score": <float 0.0–1.0>, "reasoning": "<one concise sentence>"}"""


# ---------------------------------------------------------------------------
# Rule-based scorers
# ---------------------------------------------------------------------------

def score_routing(steps: str | None, expected_routing: str) -> tuple[float, str]:
    """Check if the trace was routed to the expected agent."""
    if not steps:
        return 0.0, "No routing data available in trace metadata"

    steps_lower = steps.lower()

    if expected_routing in steps_lower:
        return 1.0, f"Correctly routed to {expected_routing}"

    # Near-miss: wrong but close (api_agent vs ui_agent)
    for expected, actual in NEAR_MISS_PAIRS:
        if expected_routing == expected and actual in steps_lower:
            return 0.5, f"Near-miss routing: expected {expected}, got {actual} (steps={steps})"

    return 0.0, f"Wrong routing: expected {expected_routing}, actual steps={steps}"


def score_answered(response_text: str) -> tuple[float, str]:
    """Check if the bot actually answered vs. returned a retrieval failure message."""
    lower = response_text.lower()
    for pattern in RETRIEVAL_FAILURE_PATTERNS:
        if pattern in lower:
            return 0.0, f"Bot failed to answer — matched failure pattern: '{pattern}'"
    return 1.0, "Bot provided an answer"


def score_link(response_text: str, expected_link_path: str | None) -> tuple[float | None, str]:
    """Check if the response contains the expected navigation link path."""
    if not expected_link_path:
        return None, "No link check configured for this case"

    url_pattern = re.compile(r'https?://[^\s<>"]+|/[\w\-/.]+(?:\?[^\s<>"]*)?')
    links = url_pattern.findall(response_text)

    if not links:
        return 0.0, f"No links found in response (expected path: {expected_link_path})"

    for link in links:
        if expected_link_path in link:
            return 1.0, f"Correct link found containing '{expected_link_path}': {link}"

    return 0.5, f"Link(s) present but none match '{expected_link_path}': {links[:3]}"


def score_guardrail(response_text: str, must_not_include: list[str]) -> tuple[float, str]:
    """Check for guardrail violations — strings that must not appear in the response."""
    if not must_not_include:
        return 1.0, "No guardrail constraints defined"

    lower = response_text.lower()
    for forbidden in must_not_include:
        if forbidden.lower() in lower:
            return 0.0, f"Guardrail violated: response contains forbidden string '{forbidden}'"
    return 1.0, "No guardrail violations detected"


def score_must_include(response_text: str, must_include: list[str]) -> tuple[float, str]:
    """Check that required strings ARE present in the response (positive guardrail)."""
    if not must_include:
        return 1.0, "No must_include constraints defined"

    lower = response_text.lower()
    missing = [s for s in must_include if s.lower() not in lower]
    if not missing:
        return 1.0, f"All {len(must_include)} required string(s) found"
    found = len(must_include) - len(missing)
    score = found / len(must_include)
    return score, f"Missing required string(s): {missing}"


def score_format(response_text: str, format_checks: dict | None) -> tuple[float | None, str]:
    """
    For dynamic cases: check structural format without requiring exact values.
    format_checks keys: contains_number, mentions_severity, mentions_scan_types,
                        mentions_exploitability, names_project
    """
    if not format_checks:
        return None, "No format checks configured"

    checks_passed = 0
    total = len(format_checks)
    issues = []

    if format_checks.get("contains_number"):
        if re.search(r'\b\d+\b', response_text):
            checks_passed += 1
        else:
            issues.append("no specific number found")

    if format_checks.get("mentions_severity"):
        severity_terms = ["critical", "high", "medium", "low", "severity"]
        if any(t in response_text.lower() for t in severity_terms):
            checks_passed += 1
        else:
            issues.append("no severity level mentioned")

    if format_checks.get("mentions_scan_types"):
        scan_types = ["sca", "sast", "container", "ai"]
        found = [t for t in scan_types if t in response_text.lower()]
        if len(found) >= 2:
            checks_passed += 1
        else:
            issues.append(f"scan types missing (found: {found or 'none'})")

    if format_checks.get("mentions_exploitability"):
        exploit_terms = ["exploitable", "exploit", "reachable", "proof of concept", "poc"]
        if any(t in response_text.lower() for t in exploit_terms):
            checks_passed += 1
        else:
            issues.append("exploitability not mentioned")

    if format_checks.get("names_project"):
        # Project names in the test org follow a pattern like "platform-bot-py-1"
        if re.search(r'\b[\w]+-[\w]+-[\w]+\b|\b[\w]+-[\w]+\b', response_text):
            checks_passed += 1
        else:
            issues.append("no project-like name found")

    score = checks_passed / total if total > 0 else 0.0
    comment = f"Format: {checks_passed}/{total} checks passed"
    if issues:
        comment += f". Issues: {'; '.join(issues)}"
    return score, comment


# ---------------------------------------------------------------------------
# LLM-as-judge scorers (Claude Sonnet with prompt caching)
# ---------------------------------------------------------------------------

def score_correctness(
    question: str,
    response: str,
    expected_answer: str,
    notes: str = "",
    client: anthropic.Anthropic | None = None,
) -> tuple[float, str]:
    """LLM-as-judge: assess factual correctness against the expected answer."""
    if client is None:
        client = anthropic.Anthropic()

    user_content = f"""Question asked to the chatbot:
{question}

Expected answer (key facts that must be present):
{expected_answer}

Actual bot response:
{response}
{f"{chr(10)}Evaluator notes: {notes}" if notes else ""}"""

    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=256,
            system=[{
                "type": "text",
                "text": JUDGE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        result = json.loads(resp.content[0].text)
        return float(result["score"]), result["reasoning"]
    except (json.JSONDecodeError, KeyError) as e:
        return 0.0, f"Judge parse error: {e} — raw: {resp.content[0].text[:200]}"
    except Exception as e:
        return 0.0, f"Judge error: {e}"


def score_coherence(
    t1_question: str,
    t1_response: str,
    t2_question: str,
    t2_response: str,
    t2_notes: str = "",
    client: anthropic.Anthropic | None = None,
) -> tuple[float, str]:
    """LLM-as-judge: assess multi-turn context retention."""
    if client is None:
        client = anthropic.Anthropic()

    user_content = f"""Turn 1 question: {t1_question}
Turn 1 bot response: {t1_response}

Turn 2 question: {t2_question}
Turn 2 bot response: {t2_response}
{f"{chr(10)}Evaluator notes: {t2_notes}" if t2_notes else ""}"""

    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=256,
            system=[{
                "type": "text",
                "text": COHERENCE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        result = json.loads(resp.content[0].text)
        return float(result["score"]), result["reasoning"]
    except (json.JSONDecodeError, KeyError) as e:
        return 0.0, f"Coherence judge parse error: {e}"
    except Exception as e:
        return 0.0, f"Coherence judge error: {e}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_all_scorers(
    case: dict,
    response_text: str,
    steps: str | None,
    anthropic_client: anthropic.Anthropic | None = None,
) -> dict[str, tuple[float, str]]:
    """
    Run all applicable scorers for a single-turn golden case.
    Returns {score_name: (value, comment)}.
    """
    dims = case.get("eval_dimensions", [])
    scores = {}

    if "routing" in dims:
        scores["routing"] = score_routing(steps, case["expected_routing"])

    if "answered" in dims:
        scores["answered"] = score_answered(response_text)

    if "link_valid" in dims:
        result = score_link(response_text, case.get("link_path"))
        if result[0] is not None:
            scores["link_valid"] = result

    if "guardrail" in dims:
        scores["guardrail"] = score_guardrail(response_text, case.get("must_not_include", []))

    if "must_include" in dims:
        scores["must_include"] = score_must_include(response_text, case.get("must_include", []))

    if "format" in dims:
        result = score_format(response_text, case.get("format_checks"))
        if result[0] is not None:
            scores["format"] = result

    if "correctness" in dims and not case.get("dynamic"):
        scores["correctness"] = score_correctness(
            question=case["question"],
            response=response_text,
            expected_answer=case["expected_answer"],
            notes=case.get("notes", ""),
            client=anthropic_client,
        )

    return scores
