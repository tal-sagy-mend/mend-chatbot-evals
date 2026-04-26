# Mend Chatbot Eval Framework — Session Context

Use this prompt to resume work on the evaluation framework. Read the whole thing before touching any file.

---

## What this project is

An automated eval framework for the **Mend Platform Chatbot** (PRD-8768). The bot is an AI assistant embedded in the Mend security platform. It answers docs questions, navigates the UI, and queries live finding data.

Goals:
1. Import a curated golden set into Langfuse as a dataset
2. Run LLM-as-judge + rule-based scoring against the live bot
3. Track score trends across bot versions
4. Grow the golden set over time using real production traces

---

## Infrastructure

| Thing | Value |
|---|---|
| Langfuse | `langfuse.mendinfra.com`, project `cmmjhinsw000b0607dpmoze5r` |
| Bot env | `dev.whitesourcesoftware.com` |
| Test org | `noa-test` (`a1a0c801-365d-47e8-855c-6818fb786cbc`) |
| Test app UUID | `9a92b5c2-b145-41e1-acfe-916e20e9b0f5` |
| MCP proxy | `http://127.0.0.1:9988` (must be running for regression runner) |
| Auth | 2-step JWT: login → refreshToken → accessToken (10-min TTL) |
| HTTP | HTTP/1.1 required — HTTP/2 returns 500 |
| User | `tal.sagy@mend.io` |

---

## Files and what they do

```
mend-chatbot-evals/
├── golden_set.json          46-case curated golden set (source of truth)
├── scorers.py               All scoring functions (rule-based + LLM judge)
├── eval_runner.py           Production mode: score existing Langfuse traces
├── regression_runner.py     Regression mode: call bot → wait → score → post
├── import_dataset.py        One-time: push golden_set.json to Langfuse dataset
├── harvest_traces.py        Harvest production traces → review_queue.jsonl
├── import_harvested.py      Annotated review_queue.jsonl → golden_set.json
├── langfuse_client.py       Langfuse SDK wrapper (fetch traces, post scores)
├── bot_client.py            MendBotClient: auth + MCP ask_assistant calls
├── config.py                Loads .env vars
├── REVIEW.md                Human sign-off tracker
└── SESSION_PROMPT.md        This file
```

---

## Golden set structure (`golden_set.json`)

**46 cases, 7 suites:**

| Suite | Cases | Notes |
|---|---|---|
| D1 | 12 | Docs questions. 3 are `gap_case: true` (D1-GS-10/11/12) — excluded from aggregate |
| N | 14 | Navigation. 8 new/untested. N-GS-03/05/06 have known routing bug (goes to api_agent). N-GS-04 link_path unverified |
| G | 4 | Greetings — all passing, low signal |
| E | 6 | Guardrails/edge cases |
| A | 6 | Dynamic (answer changes daily) — format-only scoring |
| M | 4 | Multi-turn: 2 docs-only, 1 cross-API context (M-GS-03), 1 agent-switch (M-GS-04) |
| H | 0 | Harvested from production — populated by harvest workflow |

**Case schema (key fields):**
```json
{
  "id": "D1-GS-01",
  "suite": "D1",
  "gap_case": false,          // true = excluded from aggregate score
  "dynamic": false,           // true = format-only scoring (no expected_answer)
  "multi_turn": false,
  "question": "...",
  "page_url": null,           // passed to bot as UI context; null = no page context
  "expected_routing": "docs_agent",
  "expected_answer": "...",
  "must_include": [],         // positive check (score_must_include scorer exists but not yet wired to any case)
  "must_not_include": [],     // guardrail check
  "eval_dimensions": ["routing", "answered", "correctness"],
  "link_path": null,          // substring to find in response links
  "format_checks": null,      // for dynamic cases: {contains_number, mentions_severity, ...}
  "known_status": "pass"      // pass / fail / partial / regression / new
}
```

**Multi-turn cases** have a `turns[]` array instead of a top-level question.

---

## Scorer inventory (`scorers.py`)

| Scorer | Dimension | Type |
|---|---|---|
| `score_routing` | `routing` | Rule-based. Near-miss: ui_agent↔api_agent = 0.5 |
| `score_answered` | `answered` | Rule-based. Checks RETRIEVAL_FAILURE_PATTERNS |
| `score_link` | `link_valid` | Rule-based. Substring match on response URLs |
| `score_guardrail` | `guardrail` | Rule-based. Checks must_not_include strings |
| `score_must_include` | `must_include` | Rule-based. **Exists but no case uses this dimension yet** |
| `score_format` | `format` | Rule-based. Checks format_checks dict |
| `score_correctness` | `correctness` | LLM judge (Claude Sonnet, prompt-cached) |
| `score_coherence` | `coherence` | LLM judge. Multi-turn context retention |

---

## Running things

```bash
# Import golden set to Langfuse (first time only — NOT yet run)
python import_dataset.py --dry-run
python import_dataset.py

# Run regression (call bot with every golden question, score, post to Langfuse)
# Requires: proxy running at 127.0.0.1:9988 and valid ANTHROPIC_API_KEY in .env
python3 regression_runner.py --suite D1 --dry-run   # smoke test, no Langfuse writes
python3 regression_runner.py --suite D1             # D1 only, posts scores
python3 regression_runner.py                        # full 46-case run

# Score existing production traces
python eval_runner.py --hours 24 --dry-run

# Harvest new traces for golden set expansion
python harvest_traces.py --autofill          # pre-fills everything it can
python harvest_traces.py --autofill --skip-llm  # skip LLM docs-answer generation

# Import annotated harvest records
python import_harvested.py --input review_queue.jsonl          # dry-run
python import_harvested.py --input review_queue.jsonl --apply  # write
```

---

## Harvest / annotation workflow

Goal: grow the golden set from 46 → 200+ cases using real user questions from production.

**Weekly cadence (takes ~5 min of your time):**
1. `python harvest_traces.py --autofill` — fetches last 7 days, pre-populates all fields, appends to `review_queue.jsonl` (never overwrites existing annotations)
2. Open `review_queue.jsonl`, for each record:
   - Spot-check the pre-filled fields
   - Set `"include": true` or `"include": false`
   - Correct anything wrong (routing, expected_answer, dynamic flag)
3. `python import_harvested.py --input review_queue.jsonl --apply`

**What autofill does per record:**
- `expected_routing` ← copied from detected routing
- `dynamic` ← inferred from question/response patterns
- `eval_dimensions` ← inferred from cluster
- `format_checks` ← inferred from keywords (dynamic cases only)
- `must_not_include` ← standard retrieval-failure strings
- `expected_answer` ← LLM-generated for docs (verify!), URL-extracted for nav, null for dynamic

**Dynamic cases:** never get an expected_answer — it's time-sensitive. Scored on format_checks only.

**Harvested case IDs:** `H-GS-001`, `H-GS-002`, ... (suite = "H")

---

## Key design decisions (and why)

**Gap cases excluded from aggregate**
D1-GS-10/11/12 are known docs-index gaps that always score 0. Including them in the aggregate would permanently depress the score by ~7pp and make "we regressed" indistinguishable from "the gap still exists." The regression runner reports them in a separate section.

**Dynamic vs static split**
A-suite questions (finding counts, project rankings) change daily. Scoring them for correctness would always fail after day 1. Format-only scoring checks structure (contains a number, mentions severity) without caring about the value.

**page_url on navigation cases**
The bot is page-aware: when the UI sends a `page_url` with a `product=UUID` param, the bot scopes its response to that application. N cases that test app-level navigation (N-GS-07/08) include a `page_url` with the test app UUID. The regression runner passes `page_url` to `bot.ask()`.

**N-GS-03/05/06 link_path is loose**
There is no unified findings view at org level. The expected behavior is for the bot to return links to individual application findings pages. `link_path: "applications/findings"` matches any such link — it doesn't verify the right application. The `correctness` eval_dimension (LLM judge) handles the qualitative check.

**Statistical validity**
46 cases = ±14pp confidence interval overall; suite-level scores are meaningless. This is a **diagnostic tool**, not a measurement instrument. Suitable for: catching large regressions, tracking specific known bugs, smoke testing. NOT suitable for: claiming "bot improved by 5%", meaningful suite-level comparisons. Statistical validity requires 150–200 cases (from harvest workflow) + multiple runs per case.

---

## Known open items

| Item | Priority | Notes |
|---|---|---|
| ~~Run `import_dataset.py`~~ | ~~P0~~ | **DONE 2026-04-20** — 46/46 items in Langfuse at `https://langfuse.mendinfra.com/datasets/chatbot-golden-set-v1` |
| ~~Test regression_runner end-to-end~~ | ~~P1~~ | **DONE 2026-04-21** — pipeline working. Fix ANTHROPIC_API_KEY then re-run to get correctness scores |
| Fix ANTHROPIC_API_KEY in `.env` | P1 | Key is invalid (401). All correctness scores are 0.0 until fixed. Rule-based scores (routing, answered) are correct. |
| Re-run D1 clean with LLM judge | P1 | After key fix: `python3 regression_runner.py --suite D1` — first real baseline |
| D1 routing regressions | P1 | D1-GS-01/02/03/04 routing to wrong agent (not docs_agent). May be a new bot regression vs. v1.26.0 — investigate |
| Verify N-GS-04 link_path | P1 | `dashboard/value` is tentative — open the Value dashboard and check the URL |
| Wire `must_include` eval_dimension | P2 | `score_must_include` scorer exists; no case uses it yet — add to G-GS-02 at minimum |
| N-GS-03/05/06 routing bug | P2 | These route to api_agent instead of ui_agent — known bot bug, tracking for fix |
| Start harvest cycle | P2 | Run `harvest_traces.py --autofill` for first batch |
| N-GS-07 through N-GS-14 baseline | P2 | All `known_status: new` — run once to establish baseline |
| D1 gap cases may no longer be gaps | P3 | D1-GS-11/12 returned real answers in the 2026-04-21 run — re-evaluate whether they're still `gap_case: true` |

---

## Regression runner — implementation notes

The runner was first tested end-to-end on 2026-04-21. Six bugs were fixed:

| Bug | File | Fix |
|---|---|---|
| `create_conversation` 400 error | `bot_client.py` | POST body was `b""` — changed to `b"{}"` (API requires non-empty JSON body) |
| Langfuse SDK 3.x break | `langfuse_client.py` | `client.fetch_traces()` → `client.api.trace.list()`; `client.score()` → `client.api.score.create(request=CreateScoreRequest(...))` |
| Wrong kwarg name | `regression_runner.py` | `anth_client=` → `anthropic_client=` when calling `run_all_scorers` |
| User ID separator | `config.py` | Bot writes `email_org_uuid` (underscore); config was building `email:org_uuid` (colon) |
| Routing always 0.0 | `langfuse_client.py` | `extract_steps` was reading non-existent trace metadata keys. Routing is in **observation** metadata: `langgraph_node` (values: `docs_agent`, `ui_agent`, `api_agent`). Now fetches observations via `client.api.observations.get_many(trace_id=...)` |
| "couldn't find" not detected | `scorers.py` | Added `"i couldn't find a complete answer"` to `RETRIEVAL_FAILURE_PATTERNS` |

**Proxy management:** The MCP proxy at `127.0.0.1:9988` can get stuck after days of uptime (stops responding, 0% CPU). Restart it with:
```bash
kill $(lsof -ti :9988) && python3 ~/mend-mcp-proxy/proxy.py &
```

**Langfuse trace structure:** Traces are LangGraph traces (name=`LangGraph`). Routing info is NOT in `trace.metadata` — it's in individual observation metadata as `langgraph_node`. Relevant node names: `router`, `docs_agent`, `ui_agent`, `api_agent`, `judge`. The `extract_steps` function fetches all observations for the trace and collects the agent node names.

---

## Bot version history

| Version | Pass rate | Notes |
|---|---|---|
| v1.26.0 | 40.5% (34/84) | Baseline |
| v1.27.0 | 59.5% (50/84) | +19pp. Fixed E-07, E-10. Regressed D1-07/10/13, A-12, N-06/07/08 |

Current bot: `platform-assistant v1.27.0`

---

## Platform URL format

```
Base: https://dev.whitesourcesoftware.com/app/orgs/noa-test/

General pages:   /applications, /workflows, /admin/general, /dashboard/security
App-scoped:      /applications/{view}?product={app-uuid}
Filter params:   filter_unified_findings_tbl_status=OPEN|RESOLVED
                 filter_unified_findings_tbl_engine=IMG,SAST,SCA,AI
                 filter_project_scan_summaries_tbl_scanTime=custom_date_time_filter%3Aquick_select_7_days
                 filter_violations_tbl_risk=High
                 filter_workflows_tbl_enabled=EQUALS%3Atrue
                 filter_workflows_tbl_workflowType=EQUALS%3ALEGAL
```
