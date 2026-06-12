# Roo Routing Audit — Why Skill Selection Is Brittle

**Date:** 2026-06-11 · **Scope:** `roo-standalone/` (agent.py, content_intent.py, skills/executor.py, main.py, skills/*/SKILL.md)

## TL;DR

Roo's confusion is not a tuning problem — it's architectural. The one component that can generalise (the LLM router) is consulted **last**, and only after ~174 regex/substring checks and 120 trigger keywords have had first claim on the message. Most messages never reach the LLM at all: they are captured by literal word-matching that fires on vocabulary ("article", "task", "csv", "book", "imaging") regardless of intent. When the words match the regexes' expectations, routing works; any other phrasing either falls into the wrong skill or through to a weak LLM fallback. That is exactly the "works only with precise wording" behaviour you're seeing.

## The routing funnel, as built

A single mention passes through up to **seven decision layers**, first-match-wins:

| # | Layer | Location | Mechanism |
|---|-------|----------|-----------|
| 1 | Delegation strip | `content_intent.extract_content_factory_delegation` | regex |
| 2 | "Routing intent" | `content_intent.parse_routing_intent` (agent.py:115) | ~40 regexes, hard-routes to content-factory / github |
| 3 | Fast path | `agent._try_fast_path` (agent.py:495) | exact-string regexes for points commands |
| 4 | `_looks_like_*` heuristics | agent.py:340–426 | per-skill regexes hardcoded in the shared agent, fixed priority order: luma → content → linear → points |
| 5 | Keyword scoring | agent.py:474–493 | SKILL.md `trigger_keywords`, score = `words×3 + chars`, win margin = 4 |
| 6 | LLM router | agent.py:739–776 | finally — one-line descriptions, `max_tokens=96`, `reasoning_effort="low"`, name-only output |
| 7 | Per-skill action resolution | executor.py e.g. `_resolve_points_action`:5173–5301 | ~130 lines of `if "claim" in text_lower` that **override** the LLM-extracted params |

Layers 2–5 are deterministic string matching. The LLM only sees messages that survive all of them.

## Evidence: 28 realistic phrasings through the real code

`routing_probe.py` (included alongside this report) imports the actual `content_intent.py` + SKILL.md frontmatter and replicates the agent's pre-LLM dispatch verbatim. Results for messages a community member would plausibly type:

| Message | Routed by | Routed to | Should be |
|---|---|---|---|
| "can you summarise this article for me?" | regex-intent | content-factory `action=write` | general chat |
| "what did you think of the blog post I shared?" | regex-intent | content-factory `action=write` | general chat |
| "please analyse this project proposal" | regex-intent | content-factory `action=scan` | general chat |
| "I read a research paper on keyword extraction, thoughts?" | regex-intent | content-factory | general chat |
| "connect me with someone who writes blog content" | regex-intent | content-factory `action=write` | connect-users |
| "anyone working with medical imaging?" | keywords | **medhack** (matched `imaging`) | connect-users |
| "my ecg project needs a teammate…" | keywords | **medhack** (matched `ecg`) | connect-users |
| "who's coming to the patient-data workshop?" | keywords | **medhack** (matched `patient`) | luma-events |
| "can you inspect the CSV I uploaded…" | keywords | **luma-events** (matched `csv`) | general file Q |
| "add a task to linear to fix the login bug" | keywords | **mlai-points** (matched `task`) | linear-meeting-actions |
| "what's the topic for this week's meetup?" | looks-like | content-factory (matched `topic`) | general chat |
| "rewrite this announcement in our tone of voice" | tie → LLM | ambiguous (tone 17 / medhack 15 / watt 15) | tone-of-voice |
| "do you know anyone in AI research?" | falls to LLM | — | connect-users |

10/28 misroute deterministically — before any LLM is consulted. The agent.py docstring's own example ("Do you know anyone in AI research?") only works if it survives every regex layer and the minimal LLM prompt gets it right.

## Root causes

### 1. The generalising component is the fallback, not the router
Regex can only encode phrasings someone already thought of. The LLM can generalise — but it's layer 6 of 7. So the system's *effective* behaviour is its regexes, and the LLM only handles the residue. This is inverted: deterministic shortcuts should be a thin, exact-match accelerator on top of an LLM-first router, not a 5-layer gate in front of it.

### 2. Word-level matching masquerading as intent detection
`WRITE_PATTERNS` includes bare `\barticle\b` and `\bblog\b` (content_intent.py:49–54) — so *any* sentence containing "article" hard-routes to content-factory with `action=write`, including "summarise this article". `SCAN_PATTERNS` matches any message starting with "analyse"/"inspect" plus the word "project"/"app". `mlai-points` claims bare `book`, `task`, `tasks`; `luma-events` claims bare `csv`, `attendees`, `registered`; `medhack` claims `patient`, `imaging`, `ecg`, `announcement`. Words are not intents — context decides, and only the LLM sees context.

### 3. Greedy priority ordering with no arbitration
`_select_skill_from_triggers` checks luma → content → linear → points in a fixed order; first match wins and later skills (and the LLM) never get a vote. The keyword scorer's formula (`len(words)*3 + len(chars)`, margin 4) means whichever skill stuffs longer keyword phrases into SKILL.md wins ties — an arms race, not semantics. A *single* generic keyword match auto-wins when no other skill matches anything (score 10 vs −1).

### 4. Skill knowledge is smeared across five places
For one skill you may have: `trigger_keywords` in SKILL.md, a `_looks_like_*` method in agent.py, patterns in content_intent.py, examples in the LLM router prompt, and an action-resolution chain in executor.py. They disagree with each other (e.g. "connect to" is a content-factory trigger keyword that collides with github/connect-users intent; `announce`/`announcement` are claimed by both medhack and watt-the-hack — a permanent tie). Adding or modifying a skill means editing the shared funnel, and the funnel silently steals traffic from skills that aren't hardcoded into it.

### 5. Some skills are unreachable except via the weakest layer
`connect-users` has **zero** trigger keywords, no `_looks_like_` heuristic, and no examples in the LLM router prompt. Its only path is the LLM fallback — which gets a one-line description ("Find community members with relevant expertise using vector search"), 96 max tokens, low reasoning effort, and routing rules/examples that mention five *other* skills. Meanwhile its natural vocabulary ("anyone…", "someone who…") is riddled with words other skills claim (`imaging`, `blog`, `task`). The skills users complain about most will be the ones not in the regex funnel.

### 6. The LLM's work gets overwritten downstream
Even when routing and `_extract_parameters` (executor.py:207) succeed, per-skill resolvers like `_resolve_points_action` (executor.py:5173 — ~130 lines) re-derive the action from substring checks and override the params: "submit" anywhere → `submit_task`, "approve" anywhere → `approve_task`, "claim" anywhere → `claim_task`. So "don't approve that yet, just show my tasks" can still fire `approve_task`. This is why Roo sometimes routes to the *right* skill but does the *wrong thing inside it*.

### 7. Thread stickiness pins mistakes for 2 hours
`_remember_selected_skill` + `THREAD_CONTEXT_TTL = 2h` (agent.py:25) means one misroute contaminates every follow-up in the thread: generic follow-ups ("do it", "go ahead", anything containing "write"/"draft"/"research") re-route to the remembered skill. Users experience this as Roo "stuck" on the wrong behaviour.

### 8. Channel scoping happens after routing, not during
`exclusive_channels` is enforced inside two skill executors (executor.py:907, 1005) — *after* the skill has already been selected — and `priority_channels` only appears as a soft hint in the LLM prompt (agent.py:708), which most messages never reach. So medhack keywords can capture messages anywhere in the workspace, then refuse to run because of the channel — the worst of both.

### 9. The test suite locks in brittleness
`test_agent_routing.py` asserts that *specific exact phrasings* route correctly — one phrase per behaviour. There are no negative tests ("summarise this article" must NOT go to content-factory) and no paraphrase sets. Every user complaint gets fixed by adding another regex or typo mapping (see the `coworkign`/`cowokrking` dictionary, agent.py:372) which passes its one new test and enlarges the collision surface. The system gets *more* brittle with each patch — this is the trajectory you're on.

## Why this produces exactly your symptoms

- **"Calls the wrong skill"** → generic-word capture by layers 2–5 (evidence table above).
- **"Doesn't activate the skill unless wording is precise"** → the funnel encodes blessed phrasings; anything else falls to a starved LLM fallback or to `_general_response`, which chats instead of acting.
- **"Doesn't respond correctly"** → right skill, wrong action: executor-level substring chains override LLM-extracted params; fast-path vs normal-path render different responses for near-identical inputs ("balance" vs "balance?").
- **"Getting worse over time"** → every fix adds patterns; patterns collide; collisions get fixed with more patterns.

## Recommendations (in order of leverage)

1. **Invert the architecture: LLM-first routing via tool/function calling.** Present every skill to one router call as a tool with a rich description (what it does, when to use it, when NOT to use it, 3–5 examples, parameters) — generated from SKILL.md so the file is the single source of truth. Let the model pick skill + params in one call with thread history and channel name in context. Modern small models do this reliably and cheaply; this replaces layers 2, 4, 5 and most of 7.
2. **Demote regex to exact-match shortcuts only.** Keep the fast path for literal commands (`^points$`, `^tasks$`) — that's its legitimate use. Delete `_looks_like_*`, `parse_routing_intent`'s bare-word patterns, and the keyword scorer. If a deterministic route is kept, it must require unambiguous evidence (e.g. explicit domain + verb), never a single generic noun.
3. **Move action selection inside skills to the same tool-calling pattern.** Replace `_resolve_points_action`'s substring chain with an enum parameter (`action: balance|list_tasks|book_coworking|…`) the router/extractor fills. Keyword overrides should not outrank the LLM's structured output.
4. **Make channel scoping a routing constraint.** Filter the candidate skill list by `exclusive_channels` *before* routing, and boost `priority_channels` candidates in the router prompt — then delete the post-hoc refusals in the executors.
5. **Fix the starved router config if you keep it short-term.** Include every skill in the examples (connect-users, medhack, watt, tone-of-voice are absent), pass the channel name always, raise reasoning effort, and expand descriptions. Cheap stopgap, real gains.
6. **Loosen thread stickiness.** Sticky skill should be a router *hint* ("last skill in this thread was X"), not a bypass; drop the 2h TTL to something short or require the LLM to confirm continuation.
7. **Build a routing eval set instead of one-phrase tests.** Collect real misrouted messages from Slack logs into a labelled set (message → expected skill/action, including `none`), run it in CI against the router. Then "fixing" routing means measurably improving the eval, not adding a regex that passes one test. `routing_probe.py` is a starting skeleton.

## Files reviewed

`roo/agent.py` (838 ln), `roo/content_intent.py` (246), `roo/skills/executor.py` (8,030), `roo/skills/loader.py` (243), `roo/main.py` (5,208, partial), `roo/config.py`, `roo/llm.py`, all 9 `skills/*/SKILL.md`, `roo/tests/test_agent_routing.py`. Probe script: `routing_probe.py`.
