# Roo Routing Redesign — Implementation Plan

**Date:** 2026-06-11 · **Scope:** `roo-standalone/` · **Companion:** `ROUTING_AUDIT.md`, `routing_probe.py`

> **Status (2026-06-12):** Phases 0, 1, and 2 implemented (uncommitted, on `main`'s working tree).
> Phase 0+1: eval harness `roo/routing_eval/` (174 cases) + `scripts/run_routing_eval.py` + pytest
> gates; deterministic misroutes 13 → 0; decided-accuracy 89.4% → 100%; blessed corpus intact.
> Phase 2: `chat_tools()` in llm.py; `routing:`/`actions:` blocks backfilled in all 9 SKILL.mds
> (loader-validated); `roo/router.py` (tool-calling router + respond_in_chat/ask_clarification +
> lint_catalog); `ROUTER_V2=off|shadow|on` wired into handle_mention (shadow logs
> ROUTING_DECISION_V2 with disagree flag); eval `--mode v2`; catalog lint + router unit tests.
> **Pending:** live v2 eval run (`scripts/run_routing_eval.py --mode v2`) needs an OPENAI_API_KEY
> (e.g. in roo-standalone/.env) — then tune descriptions to ≥95%/≥90% targets, enable shadow in prod.

## Goal

Roo should pick the right skill and the right action inside it from *meaning*, not vocabulary. A community member should be able to phrase a request any reasonable way and get the right behaviour; fixing a misroute should mean editing a description or adding an eval case — never adding a regex.

**End state in one sentence:** every non-trivial mention is routed by a single LLM tool-calling pass over a skill catalog generated from SKILL.md files, with regex surviving only as exact-match shortcuts, channel scoping applied *before* routing, executors trusting the router's structured output, and a labelled eval set in CI proving it all works.

### Success criteria (measurable)

| Metric | Target | Measured by |
|---|---|---|
| Skill selection accuracy on eval set | ≥ 95% | eval runner (Phase 0) |
| Action selection accuracy (right skill *and* right action) | ≥ 90% | eval runner |
| The 10 deterministic misroutes in `routing_probe.py` | 10/10 fixed | probe cases ported into eval set |
| Existing blessed phrasings (all of `test_agent_routing.py`) | 100% still pass | ported into eval set as regression cases |
| Paraphrase robustness: ≥ 8 phrasings per skill that appear nowhere in SKILL.md text | ≥ 90% | eval runner |
| Misroute fix workflow | description/eval edit only, no `agent.py`/`executor.py` change | code review convention + lint |

---

## Target architecture

```
Slack event
  └─ normalize + strip Roo mention                       (keep as-is)
  └─ delegation clause parse ("… as <@U123>")            (keep — it's authz, not routing)
  └─ FAST PATH: exact-match commands only                (keep — ^points$, ^tasks$, …)
  └─ ROUTER (new, one LLM call):
       inputs:  message, channel name, thread history (last ~6),
                thread hint (last skill/domain/job), attached-file metadata
       tools:   one per skill, generated from SKILL.md
                (filtered by exclusive_channels BEFORE the call)
                + respond_in_chat (general conversation — a first-class choice)
                + ask_clarification (optional)
       output:  RouteDecision { skill, action, params, reason }
  └─ EXECUTOR: dispatch on (skill, action), validate params against the
       skill's declared schema, ask for missing required params.
       No re-derivation of action from substrings.
```

Four load-bearing decisions:

1. **One call does skill + action + params.** The router replaces both `_select_skill` (agent.py:675) *and* `_extract_parameters` (executor.py:207). Today most skill paths already burn two sequential LLM calls (route → extract); the new path is one call with structured output, so net latency and cost go *down*, not up.
2. **SKILL.md is the single source of truth.** Routing knowledge currently lives in five places (SKILL.md keywords, `_looks_like_*` in agent.py, patterns in content_intent.py, examples in the router prompt, substring chains in executor.py). All of it collapses into a structured `routing:` + `actions:` block in each SKILL.md, validated at startup. Adding a skill = adding a directory; no shared-funnel edits.
3. **Deterministic code survives only where determinism is the point:** the fast path (exact strings, agent.py:495–554), delegation stripping (content_intent.py:122–154), and Slack interactive-button callbacks that re-enter `handle_mention` with explicit `param_overrides` (main.py:3235, 3281, 3370 — these bypass intent inference by design and must keep working).
4. **Eval before everything.** Phase 0 builds the measurement harness first, so every later phase lands with a number attached. The current test style (one exact phrase per behaviour, no negatives) is what locked the brittleness in; the eval set is its replacement.

---

## Phase 0 — Measurement first: eval harness + decision logging

*Why first: you cannot safely delete 174 regexes without a net. Also produces the baseline number that justifies the rest.*

**Build `roo/routing_eval/`** (new):

- `cases/*.yaml` — labelled routing cases. Schema:

  ```yaml
  - id: connect-medical-imaging
    text: "anyone in the community working with medical imaging?"
    channel: general            # optional; router receives channel name
    thread:                     # optional thread context / prior turns
      last_skill: null
    files: []                   # optional attached-file metadata
    expect:
      skill: connect-users     # or "none" for general chat
      action: search            # optional
      params_subset: {query: "medical imaging"}   # optional, subset match
    tags: [paraphrase, audit-misroute]
  ```

- Seed sources, in order:
  1. the 28 cases in `routing_probe.py` (10 are known deterministic misroutes — tag them),
  2. every phrasing asserted in `roo/tests/test_agent_routing.py` (~40 — these are the regression floor),
  3. real misrouted messages pulled from Slack (search Roo's history for threads where users rephrased or corrected it),
  4. 8–10 generated paraphrases per skill, human-reviewed, **including negative cases** ("summarise this article" → `none`) and cross-skill traps ("add a task to linear…" → linear-meeting-actions, not mlai-points).
  Target ≈ 150–200 cases at Phase 0, growing forever after.

- `scripts/run_routing_eval.py` — runner with two modes:
  - `--deterministic` — replays cases through the *current* pre-LLM funnel only (the logic `routing_probe.py` already replicates; import the real functions). Hermetic, runs on every PR.
  - `--full` — calls the real router (v1 now, v2 later) with live LLM. Run nightly and on any change to `skills/**`, `router.py`, or prompts. ~200 cases on a mini-class model costs cents.
  - Output: per-skill precision/recall, action accuracy, confusion matrix, and a diff vs a checked-in `baseline.json`. Non-zero exit on regression.

**Add routing decision logging** in `RooAgent.handle_mention` (agent.py:56): one structured JSON line per mention — `{text, channel, layer: fast|intent-regex|looks-like|keywords|llm|none, skill, action, params, latency_ms}`. Optionally mirror to a private `#roo-routing-log` channel. This is where future eval cases come from, and it's how shadow mode (Phase 2) gets compared.

**Acceptance:** baseline accuracy recorded for the current system; CI runs deterministic mode; full mode runnable on demand.

---

## Phase 1 — Quick wins on the existing funnel (stop the worst bleeding)

*Why: Phase 2 takes days; these are hours and remove the most embarrassing misroutes immediately. All are gated by the Phase 0 eval.*

1. **Delete single-generic-noun trigger keywords.** In `skills/*/SKILL.md`: mlai-points loses `book`, `task`, `tasks`; luma-events loses `csv`, `attendees`, `attendee`, `guests`, `registered`, `registrations`; content-factory loses `connect to`, `blog post`, `article topic`; tone-of-voice loses bare `tone`, `rewrite`, `rephrase` → replace with multiword phrases (`tone of voice`, `rewrite this in`, `our tone`). Multiword, high-precision phrases stay.
2. **Scope exclusive-channel skills' keywords to their channels.** In `_select_skill_from_triggers` (agent.py:437), skip any skill whose `exclusive_channels` doesn't include the current channel. This alone stops `imaging`/`ecg`/`patient`/`announcement` capturing messages workspace-wide. (Requires resolving channel name once in `handle_mention` — already available via `get_channel_name`.)
3. **Defang the bare-noun WRITE_PATTERNS.** In content_intent.py:49–54, remove `\barticle\b` and `\bblog(?:\s+post)?\b` as standalone routes; require verb+object (`write/generate/draft … article|blog|content`). Same for `SCAN_PATTERNS`' bare `^analyse|inspect` start-anchor (line 19) — require an explicit target (repo/codebase/domain/site/URL).
4. **Feed the starved LLM fallback** (agent.py:739–776): include all 9 skills in the examples (connect-users, medhack, watt-the-hack, tone-of-voice are currently absent), add 2–3 `none` examples, always pass the channel name, and add `when NOT to use` lines for content-factory (summaries/opinions of existing content → none).
5. **Loosen thread stickiness slightly:** in `_select_skill_from_triggers` (agent.py:465–472), the content follow-up branch fires on bare `\bwrite\b|\bresearch\b` etc. for 2 hours; require the message to *not* match another skill first (it already half-does this for points) and drop `THREAD_CONTEXT_TTL` to 30–45 min as an interim value.

Some existing tests assert exactly the behaviour being removed — update them deliberately as part of this phase, moving the phrasings into the eval set with corrected labels.

**Acceptance:** eval accuracy strictly improves; all kept regression cases pass; the worst audit examples ("anyone working with medical imaging?", "can you inspect the CSV…", "add a task to linear…") stop misrouting deterministically (most will now fall through to the LLM layer, which is the point).

---

## Phase 2 — The tool-calling router, behind a flag (the core build)

### 2a. Tool-calling support in `llm.py`

`OpenAIClient.chat` only returns text today. Add:

```python
async def chat_tools(self, messages, tools, *, tool_choice="required",
                     model=None, reasoning_effort=None) -> ToolCallResponse
# ToolCallResponse: { name: str, arguments: dict, raw: Any }
```

- OpenAI + Gemini (OpenAI-compat layer) both support function calling with `strict` JSON-schema mode — that covers the two providers Roo actually runs on. Add the Anthropic equivalent (tool use blocks) only if/when needed.
- While in there: `AnthropicClient.chat` silently ignores `model`/`max_tokens` kwargs and pins `claude-3-5-sonnet-20241022` — fix or document, since `ROUTER_MODEL` would silently not apply on that provider.
- Parse failures: retry once with the validation error appended; on second failure raise to the router's fallback path.

### 2b. SKILL.md schema extension + loader validation

Extend frontmatter (parsed in `loader.py`, validated with a pydantic model so startup **fails loudly** on malformed manifests):

```yaml
name: mlai-points
description: >
  Manage the MLAI Roo-points system: balances, point history, claimable tasks,
  coworking bookings, rewards, top-ups, and points-admin management.
routing:
  use_when: >
    The user wants to check or spend THEIR points, manage claimable community
    tasks (claim/submit/approve), book or cancel coworking, browse or request
    rewards, buy top-up packs, or administer the points system.
  avoid_when: >
    "Task"/"ticket" in the context of Linear or meetings (→ linear-meeting-actions).
    Booking rooms or non-coworking logistics. Questions about event attendance (→ luma-events).
  examples:
    - {text: "how do I earn points?", action: list_tasks}
    - {text: "book me in for coworking tomorrow", action: book_coworking, params: {date: "<resolved date>"}}
    - {text: "can you approve ROO-42", action: approve_task, params: {task_id: "ROO-42"}}
  negative_examples:
    - {text: "add a task to linear to fix the login bug", instead: linear-meeting-actions}
    - {text: "what's on at coworking this week?", instead: none}
actions:
  - name: balance
    description: Show the user's current points balance.
  - name: list_tasks
    description: List claimable/open community tasks (modes: open, mine, review, all).
    params:
      mode: {type: string, enum: [open, mine, review, all], default: open}
  - name: book_coworking
    description: Book the user a coworking spot for a date.
    params:
      date: {type: string, format: date, required: true}
  # … one entry per action the executor actually implements:
  # balance, history, list_tasks, claim_task, unclaim_task, submit_task,
  # create_task, cancel_task, edit_task, approve_task, reject_task,
  # book_coworking, cancel_coworking, check_coworking, admin_checkin_coworking,
  # coworking_report, list_rewards, request_reward, view_rate_card,
  # topup_points, request_points, award_points, deduct_points,
  # promote_points_admin, revoke_points_admin, set_points_admin_allowance
channels:
  priority: []        # renamed from priority_channels
  exclusive: []       # renamed from exclusive_channels (keep old keys working)
```

`trigger_keywords` becomes deprecated (loader warns, then Phase 3 removes). The action list above is the ground truth already encoded in `_resolve_points_action` (executor.py:5173–5301) and `_handle_points_action` (executor.py:6796) — **backfilling all 9 SKILL.md files from their executors is the biggest single task in this phase.** Per skill: content-factory (scan/scaffold/research/write/publish_pr + domain/topic params), github-integration (reconnect/scan), linear-meeting-actions (extract/create/approve/reject + hints), luma-events (attendee_report/export_csv + count/date), medhack (event_qa/game_guess/announce…), watt-the-hack (announce/event_qa), tone-of-voice (rewrite), connect-users (search + query).

Also add a **catalog lint** (runs at startup and in CI): no example phrase may map to two skills; every skill has ≥ 3 positive and ≥ 1 negative example; description token budget per skill (keep the whole catalog ≲ 4k tokens).

### 2c. `roo/router.py` (new module)

```python
@dataclass
class RouteDecision:
    skill: Optional[str]      # None => general chat
    action: Optional[str]
    params: dict
    reason: str               # model's one-line justification, for logs
    source: str               # "fast" | "router" | "fallback"

async def route(text, *, channel_name, thread_history, thread_hint,
                files, skills) -> RouteDecision
```

- Builds one tool per catalog skill: `description` = composed `use_when` + `avoid_when` + examples; `parameters` = `{action: enum[...], params per action}` (flattened with conditional requireds, or a two-level schema — pick whichever the strict mode of both providers accepts; flattened-with-enum is the safe choice).
- Always includes `respond_in_chat(reply_style)` — general conversation is a first-class tool, not a fallback — and `ask_clarification(question)` for genuine ambiguity. This converts "wrong guess" failures into a one-line question, which users read as intelligence rather than failure.
- **Filters tools by channel before the call** (exclusive channels — Phase 5 wires it fully) and appends a context line for priority channels ("you are in #medhack-frontiers; medhack requests are likely").
- Context block: channel name, last ~6 thread turns, thread hint (`last skill / domain / active job`), attached-file names+types, current date.
- The user message goes in the **user role**, never interpolated into the system prompt — the tool schema constrains output, which is the prompt-injection story for routing.
- Config: `ROUTER_MODEL` stays configurable (current `gpt-5.4` works; benchmark a mini-tier model with the eval — with rich tool descriptions a small model usually matches the big one here, and the eval decides with data, not vibes). Replace `max_tokens=96, reasoning_effort="low"` with sane values (tool-call output is small but don't starve it; effort medium to start).
- Failure path: tool parse failure → one retry → `RouteDecision(skill=None, source="fallback")` → general response. LLM outage → same, plus the fast path still works for power users.

### 2d. Wire-in behind a flag

`config.py`: `ROUTER_V2: str = "off"` (`off | shadow | on`).

- `shadow`: v1 decides and acts as today; v2 runs fire-and-forget afterwards and logs both decisions to the Phase 0 decision log with a `disagree: true|false` field. Zero user impact; doubles router-LLM spend temporarily — time-box to 1–2 weeks.
- `on`: v2 decides; v1 funnel skipped.

**Acceptance:** v2 beats baseline on the full eval (≥ 95% skill / ≥ 90% action), including all 10 probe misroutes and 100% of regression cases; shadow-mode disagreement review over ≥ 1 week of real traffic shows v2 right (or arguably right) in the overwhelming majority of disagreements. Tune SKILL.md descriptions — not code — until this holds.

---

## Phase 3 — Cutover and funnel demolition

Flip `ROUTER_V2=on` in prod, watch the decision log for a few days, then delete the dead weight in one PR (it's all unreachable once v2 is on):

| Delete | Location |
|---|---|
| `_looks_like_content_request`, `_looks_like_points_request`, `_looks_like_luma_request`, `_looks_like_linear_meeting_request`, `_looks_like_content_follow_up`, `_normalize_points_routing_text` (agent copy) | agent.py:340–426 |
| `_select_skill_from_triggers` + keyword scorer + `_keyword_matches` | agent.py:428–493 |
| `_select_skill`'s prompt-based LLM fallback (replaced by `router.route`) | agent.py:675–791 |
| `parse_routing_intent` and its pattern tables (`SCAN/SCAFFOLD/RESEARCH/PUBLISH_PR/WRITE/GITHUB_AUTH_PATTERNS`, thread follow-up patterns) | content_intent.py:18–72, 157–246 |
| `trigger_keywords` from all SKILL.md + the `Skill` dataclass field | skills/*, loader.py |

Keep: `normalize_slack_text`, `extract_domain` (still useful as param validators/enrichers), `extract_content_factory_delegation` (authz), the fast path, and the `param_overrides` merge in `handle_mention` (button callbacks depend on it).

**Thread stickiness becomes a hint:** `_remember_selected_skill`'s workflow-guessing regexes (agent.py:255–266) are replaced by storing the router's actual `RouteDecision`; `_get_thread_context` output is *passed to the router* as the thread-hint block instead of short-circuiting routing. The router seeing "last skill: content-factory, domain: woofya.com.au, active job: 123" plus the message "go ahead" routes follow-ups correctly without a bypass. Content-factory delegation identity stickiness (agent.py:96–110) stays — that's authorization plumbing.

**Tests:** port every assertion in `test_agent_routing.py` that encodes a *phrasing* into the eval set; keep pure-unit tests for the fast path, delegation parsing, catalog building/filtering, and RouteDecision parsing. `test_llm_router_uses_gpt_5_4` becomes a config test on `router.py`.

---

## Phase 4 — Action and param integrity inside executors

*Right skill, wrong action is the second half of the bug report. The router now emits a validated action — executors must stop second-guessing it.*

Order by offender size:

1. **mlai-points.** Delete `_resolve_points_action` (executor.py:5173–5301). `_execute_mlai_points` takes `decision.action` directly; unknown/missing action → ask which of the likely actions was meant (cheap clarify). Keep as *explicit validation guards*, not text-sniffing: admin-only actions check `_is_points_admin*`; `book_coworking` with other-user mentions still flips to `admin_checkin_coworking` (that one is a safety rule, fine to keep — but implement it on `params["mentions"]`, not raw text). The supporting `_is_task_list_request`/`_match_task_list_mode`/typo-dictionary helpers go away with it; date/ID *parsers* (`_extract_task_identifier`, coworking date resolution) survive as param normalizers fed from router params.
2. **content-factory.** `action` now arrives from the router (`scan|scaffold|research|write|publish_pr`); the `_should_prompt_for_article_direction` flow keys off missing `topic` param rather than re-classifying text. `detect_content_action` is deleted with content_intent.py's patterns.
3. **watt-the-hack / medhack.** Replace the in-handler keyword checks (`announce_keywords`, executor.py:914–916) and `_classify_medhack_intent` (executor.py:1384) with router actions (`announce` vs `event_qa` vs `game_guess`). The bespoke gpt-4o-mini title/body extraction (executor.py:934) folds into the `announce` action's params (`title`, `body`) — one less LLM call.
4. **luma-events / linear-meeting-actions / tone-of-voice / connect-users.** Mostly already param-driven; seed their resolvers (`_resolve_luma_event_count/date`, linear `action`) from router params first, text-parsing second.
5. Delete the now-redundant `_extract_parameters` LLM call (executor.py:207–255) for all skills whose params the router fills (i.e., all of them). One fewer LLM round-trip per mention.

Each skill migrated adds action-accuracy eval cases (including the audit's "don't approve that yet, just show my tasks" → `list_tasks` trap case).

---

## Phase 5 — Channel scoping done right

- Resolve channel name once in `handle_mention`, pass to `router.route`; filter `channels.exclusive` skills out of the tool catalog pre-call (already stubbed in Phase 2c).
- When an exclusive skill was filtered out, append one line to the general-chat context: "the medhack skill exists but only works in #medhack-frontiers" — so Roo says *go ask there* instead of pretending the skill doesn't exist.
- Delete the post-hoc refusals in `_execute_medhack` (executor.py:1005–1013) and `_execute_watt_the_hack` (executor.py:907–912), or downgrade them to never-expected-to-fire assertions.
- `channels.priority` becomes a router context line only (already done in 2c); remove the old `channel_priority_hint` plumbing.

---

## Phase 6 — The improvement loop (make it stay good)

- **Triage cadence:** weekly skim of the routing decision log (or `#roo-routing-log`); every confirmed misroute becomes an eval case in the same PR that fixes it (description edit). The eval set only grows.
- **CI gates:** deterministic suite on every PR; full eval (live LLM) nightly and required on PRs touching `skills/**`, `router.py`, `llm.py`, or prompts; merge blocked on `baseline.json` regression. Catalog lint (collisions, example minimums, token budget) on every PR.
- **Skill authoring guide** (`docs/skill-authoring.md`): adding a skill = directory + SKILL.md with routing/actions blocks + ≥ 10 eval cases (incl. negatives) + executor handler keyed on actions. Explicitly: *no edits to agent.py/router.py are allowed for routing behaviour.*
- Optional polish, post-stabilization: lightweight feedback capture (👎 reaction on a Roo reply files the exchange into a triage queue); periodic paraphrase-generation pass to grow eval coverage; revisit `ROUTER_MODEL` with eval data (accuracy vs latency vs cost).

---

## Cost, latency, and failure posture

- **Calls per mention, today:** router (96 tok) + `_extract_parameters` + frequent per-skill extraction calls (watt, coworking-report, medhack classify) = 2–3 sequential LLM calls. **After:** 1 router call (+ whatever the skill itself genuinely needs, e.g. linear candidate extraction). Latency should be net neutral-to-better; the Slack handler already acks immediately (`asyncio.create_task`, main.py:1595), so the 3s Slack deadline is not at risk.
- **Tokens per route:** catalog ≈ 3–4k (budget-linted) + context ≈ 1k + output ≈ 100. On a mini-tier model this is ~fractions of a cent per mention; even on `gpt-5.4` it's low single cents. Shadow mode doubles this temporarily.
- **Failure posture:** LLM down → fast path still works, everything else gets a friendly general-fallback + log line. No regex emergency fallback — that's the road back to the funnel. If availability becomes a real problem, add a second provider via the existing multi-provider `llm.py` rather than resurrecting keywords.

## Risk register

| Risk | Mitigation |
|---|---|
| Regression on currently-working phrasings | Entire `test_agent_routing.py` corpus ported to eval as a hard floor before cutover; shadow mode on real traffic |
| Router model flakiness / schema violations | strict mode + one retry + clarify/general fallback; eval tracks violation rate |
| SKILL.md descriptions drift or collide over time | startup lint + CI catalog checks + eval gate |
| Token cost creep as skills grow | per-skill token budget in lint; eval measures cost per route |
| Shadow mode doubles spend | time-boxed 1–2 weeks |
| In-memory thread hints lost on restart | acceptable (hints only); note in docs |
| Button-callback flows break if `param_overrides` contract changes | explicit regression tests in Phase 2; overrides continue to merge after routing |

## Effort estimate

| Phase | Size | Notes |
|---|---|---|
| 0 — eval harness + logging | 1–2 days | mostly dataset curation |
| 1 — quick wins | 0.5–1 day | config/pattern edits + test updates |
| 2 — router build | 3–5 days | 2b backfilling 9 SKILL.mds is the long pole; 2a/2c are mechanical |
| 3 — cutover + deletion | 1–2 days | mostly deletions + test migration |
| 4 — executor integrity | 2–4 days | points is the big one |
| 5 — channel scoping | 0.5 day | |
| 6 — loop setup | 0.5 day + ongoing | |

≈ 2 weeks focused work, shippable at every phase boundary; Phases 0+1 alone deliver visible relief in ~2 days.

## File-by-file change map

| File | Phase | Change |
|---|---|---|
| `roo/routing_eval/` (new) | 0 | dataset + runner + baseline.json |
| `roo/agent.py` | 0,1,2,3 | decision logging; trigger scoping; `route()` wire-in; delete layers 2/4/5 funnel (≈ 300 lines) |
| `roo/content_intent.py` | 1,3 | defang bare-noun patterns; then delete all pattern routing, keep normalize/domain/delegation (≈ 150 lines gone) |
| `roo/llm.py` | 2 | `chat_tools()` with strict schemas; Anthropic client kwarg fix |
| `roo/router.py` (new) | 2 | catalog→tools, context assembly, RouteDecision |
| `roo/skills/loader.py` | 2,3 | pydantic-validated routing/actions schema; deprecate then drop `trigger_keywords` |
| `skills/*/SKILL.md` (×9) | 1,2 | keyword pruning; full routing/actions backfill |
| `roo/config.py` | 2 | `ROUTER_V2` flag; router tuning knobs |
| `roo/skills/executor.py` | 4,5 | execute on (action, params); delete `_resolve_points_action` + per-skill substring chains + `_extract_parameters` + post-hoc channel refusals (≈ 400+ lines) |
| `roo/tests/test_agent_routing.py` | 3 | phrasings → eval set; keep fast-path/delegation/catalog unit tests |
| `docs/skill-authoring.md` (new) | 6 | the new contract |

## Definition of done

1. All success-criteria metrics green on the eval set, in CI.
2. `grep -rn "trigger_keywords\|_looks_like\|_resolve_points_action" roo/ skills/` returns nothing.
3. A new skill can be added end-to-end (SKILL.md + executor handler + eval cases) with zero edits to agent.py/router.py — proven by doing it once (connect-users' enrichment is a good candidate since it currently has no routing metadata at all).
4. One month post-cutover: misroute reports handled exclusively via description/eval edits.
