"""
Simulated Patient endpoint logic — Slack-free.

Powers the health-hack 3D ward "Guess the Diagnosis" interaction: a player walks
up to a patient in the game world, asks a question, and this module produces an
in-character reply using the medhack skill's clinical case files.

Two personas share the endpoint via ``role``:
  - "patient" (default): the PQM narrator voicing the patient in cubicle 3.
    Guesses are classified and adjudicated.
  - "nurse": the reception nurse who reads out investigation results (bloods,
    imaging, ECG, obs) from the same case file. Never adjudicates guesses —
    the classifier is skipped entirely for this role.

This module is deliberately independent of the Slack executor (whose patient
simulator is currently DISABLED — see roo/skills/executor.py) and of the medhack
game state. It reuses ONLY pure, local logic:

  - the case YAML load (copied ~10 lines to avoid touching backend init)
  - the fuzzy-match verdict (copied from MedHackClient._fuzzy_match)
  - the verbatim PQM system prompt (recovered from
    `git show 2427ff6^:roo-standalone/roo/skills/executor.py`)
  - the guess classifier prompt (copied from executor._classify_medhack_intent)

The web game is stateless: it NEVER mutates medhack game state, never awards
points, and never enforces the one-guess lockout (out of scope — there is no
penalty for a wrong guess here).
"""
from __future__ import annotations

import json as _json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import get_settings
from .llm import get_llm_client

# cases.yaml lives alongside the medhack skill. Resolve relative to this file so
# the load works regardless of the process cwd.
CASES_FILE = Path(__file__).resolve().parent.parent / "skills" / "medhack" / "cases.yaml"

# Fields that must NEVER reach the LLM prompt. `hints` is authored coaching that
# names/points at the answer (the original Slack game only ever revealed
# hints[:hint_level]; stateless here means hint_level 0, i.e. none).
_SECRET_FIELDS = ("diagnosis", "acceptable_answers", "hints")

# Defensive caps (also enforced at the HTTP layer, belt-and-braces here).
MAX_HISTORY_TURNS = 12


# ---------------------------------------------------------------------------
# Case loading (copied from MedHackClient._load_cases — pure, no backend init)
# ---------------------------------------------------------------------------

def _load_all_cases() -> list[dict]:
    """Load every case from cases.yaml (pure local read, never cached globally)."""
    with open(CASES_FILE, "r") as f:
        data = yaml.safe_load(f)
    return data.get("cases", [])


def load_case(case_id: Optional[int]) -> dict:
    """Return the full case dict (INCLUDING secrets) for a given case_id.

    When ``case_id`` is None, fall back to the first case in cases.yaml. We do
    NOT read medhack's active-case game state — the web game is stateless and
    must never disturb the Slack game. The first case (id 1, "Salt & Static")
    is a stable, sensible default.

    Raises KeyError when a specific case_id is requested but not found (the HTTP
    layer maps this to 404).
    """
    cases = _load_all_cases()
    if not cases:
        raise KeyError("no cases available")
    if case_id is None:
        return cases[0]
    # Coerce so a string id from a JSON body (e.g. "1") still matches int ids.
    try:
        case_id = int(case_id)
    except (TypeError, ValueError):
        raise KeyError(f"invalid case_id: {case_id!r}")
    for case in cases:
        if case.get("id") == case_id:
            return case
    raise KeyError(f"unknown case_id: {case_id}")


def case_for_prompt(case: dict) -> dict:
    """Strip the secret fields (diagnosis, acceptable_answers, hints) before the LLM sees it."""
    return {k: v for k, v in case.items() if k not in _SECRET_FIELDS}


def _redact_answer(case_text: str, case: dict) -> str:
    """Redact the primary diagnosis string from the serialized case text.

    Defense-in-depth: some case notes editorialise the answer verbatim
    (e.g. investigations.*.note = "...pathognomonic for salicylate toxicity"),
    so even with the diagnosis KEY stripped the answer can sit in free text and
    be echoed under prompt injection. We redact the primary `diagnosis` string
    only — NOT the acceptable-answer synonyms, which can legitimately appear as
    discoverable findings the player earns by ordering the right test.
    """
    dx = (case.get("diagnosis") or "").strip()
    if not dx:
        return case_text
    return re.sub(re.escape(dx), "[diagnosis withheld]", case_text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Guess handling
# ---------------------------------------------------------------------------

def check_guess(guess: str, case: dict) -> bool:
    """Whether ``guess`` matches the case diagnosis.

    Self-contained copy of MedHackClient._fuzzy_match: exact match against
    acceptable_answers, then 0.75 SequenceMatcher against the primary diagnosis
    and each acceptable answer. We do NOT import the executor or the client.
    """
    guess_clean = str(guess).strip().lower()
    # Coerce defensively: a malformed case (null/non-string values) must not
    # crash the whole turn.
    acceptable = [str(a).lower() for a in (case.get("acceptable_answers") or [])]
    diagnosis = (case.get("diagnosis") or "").lower()

    # Exact match against acceptable answers
    if guess_clean in acceptable:
        return True

    # Fuzzy match against the primary diagnosis
    if diagnosis and SequenceMatcher(None, guess_clean, diagnosis).ratio() >= 0.75:
        return True

    # Fuzzy match against acceptable answers
    for answer in acceptable:
        if SequenceMatcher(None, guess_clean, answer).ratio() >= 0.75:
            return True

    return False


async def classify_guess(question: str) -> dict:
    """Classify whether a message is a diagnosis guess.

    Returns {"is_guess": bool, "diagnosis": str | None}. Prompt copied verbatim
    from executor._classify_medhack_intent (gpt-4o-mini, max_tokens=100).
    """
    prompt = f"""You are classifying messages in a medical diagnosis guessing game. Players interact with a simulated patient and can ask questions or guess the diagnosis.

A message is a DIAGNOSIS GUESS if the player is proposing what they think the medical diagnosis is. Examples:
- "I think it's pneumonia" → guess: "pneumonia"
- "Is it gastroenteritis?" → guess: "gastroenteritis"
- "I guess Addison's disease" → guess: "Addison's disease"
- "She has COPD" → guess: "COPD"
- "Could it be lupus?" → guess: "lupus"
- "My diagnosis is acute appendicitis" → guess: "acute appendicitis"
- "gastroenteritis!" → guess: "gastroenteritis"

NOT a guess (these are clinical questions or requests):
- "What are her vitals?"
- "Can I see the blood results?"
- "Does she have any allergies?" (asking about patient history, not guessing)
- "Order a chest X-ray"
- "Tell me about her symptoms"
- "What medications is she on?"

Classify this message: "{question}"

Respond with ONLY valid JSON, no markdown:
{{"is_guess": true, "diagnosis": "the diagnosis"}} or {{"is_guess": false, "diagnosis": null}}"""

    openai_client = get_llm_client("openai")
    response = await openai_client.chat(
        [{"role": "user", "content": prompt}],
        model="gpt-4o-mini",
        max_tokens=100,
    )

    try:
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = _json.loads(content)
        if not isinstance(parsed, dict):
            return {"is_guess": False, "diagnosis": None}
        diag = parsed.get("diagnosis")
        return {
            "is_guess": bool(parsed.get("is_guess")),
            # The model occasionally emits a non-string (array/number); coerce
            # to None so check_guess never receives a non-string.
            "diagnosis": diag if isinstance(diag, str) else None,
        }
    except (ValueError, KeyError, IndexError):
        return {"is_guess": False, "diagnosis": None}


# ---------------------------------------------------------------------------
# In-character reply
# ---------------------------------------------------------------------------

# Verbatim PQM system prompt recovered from
# `git show 2427ff6^:roo-standalone/roo/skills/executor.py` (_medhack_llm_response),
# with one game-world adaptation appended (the in-game dialogue-box length line).
_PQM_SYSTEM_PROMPT = """You are the MedHack Patient Quest Master (PQM), a narrator and storyteller in a fast-paced emergency department roleplay game. Your job is to present a simulated patient case to players (participants) who will ask you questions as if they are clinicians. You must answer only what the players ask, while keeping the mystery alive. You are not giving real medical advice. This is a fictional case simulation.

IMPORTANT: You know ONLY about the patient in the CASE FILE below. You have NO memory of any previous patients or cases. There is only one patient: the one described in today's case file. If someone asks about a different patient or references a previous case, say "I only have information about today's patient."

TONE AND STYLE
- You are a dungeon-master style narrator: vivid, concise, engaging.
- Describe what the clinician sees, hears, and notices.
- When the patient speaks, narrate how they say it and include their words in quotes.
- Keep answers punchy. Add small character moments. Avoid long lectures unless asked.
- The patient is a medium-to-poor historian: they ramble, minimize, and sometimes answer slightly off-target. They can still be guided with good questions.

GAME RULES
1) Only reveal information if asked. Do not volunteer findings.
2) Maintain internal consistency with the case file. Never contradict your own results.
3) If players ask for vitals, exam findings, or investigations, provide the relevant results from the case file. If they ask to "order" a test, respond as narrator: "You order X… results return: …"
4) NEVER reveal or hint at the diagnosis directly. The diagnosis is checked separately.
5) If asked about something not in the case data, provide a reasonable normal/unremarkable finding.
6) Hidden information (patient backstory, concealed history, endocrine tests) must remain hidden unless a player earns it by asking the right questions.
7) If the patient has history_disclosure_rules in their data, follow those rules for how and when to reveal sensitive history.
8) If the player tries to force the answer ("tell me the diagnosis"), refuse playfully and prompt them to keep investigating.
9) If asked about management ("what should we do?"), describe what the ED team would typically do in broad strokes (fluids, glucose, addressing electrolytes, contacting seniors). Do not give step-by-step dosing instructions.

Your replies appear in a small in-game dialogue box: keep them under ~120 words."""


# Display name for the reception-nurse persona (also the in-game name tag).
NURSE_NAME = "Nurse Priya"

_NURSE_SYSTEM_PROMPT = """You are playing Nurse Priya, the charge nurse working the reception desk in a fast-paced emergency department roleplay game. Players (participants acting as clinicians) come to your desk to ask for investigation results — bloods, imaging, ECGs, observations — for the patient in cubicle 3. You are not giving real medical advice. This is a fictional case simulation.

IMPORTANT: You know ONLY about the patient in the CASE FILE below. You have NO memory of any previous patients or cases. There is only one patient: the one described in today's case file. If someone asks about a different patient, say "I've only got today's patient on the board."

TONE AND STYLE
- Warm, quick, dry-witted, efficient — you have three other jobs on the go.
- Speak in first person, a couple of short sentences at a time.
- Read results out plainly; you may flag an abnormal value ("that potassium's up") but never interpret further than a nurse would.

GAME RULES
1) Only reveal results when asked. Do not volunteer findings.
2) When asked for a specific investigation, read the relevant results from the case file accurately — never round, embellish, or invent values. If they "order" a test that has results in the file, respond: results are back, then read them.
3) If a case-file note restricts when a result may be revealed (e.g. only if a specific test is ordered), follow that note exactly.
4) If asked for an investigation NOT in the case file, give a brief, reasonable normal/unremarkable result — one that points neither toward nor away from any diagnosis. Never contradict earlier results.
5) NEVER reveal, hint at, confirm, or deny the diagnosis. If the player proposes a diagnosis or asks what you think it is, deflect warmly ("that's your call, doc") and suggest they put it to the patient in cubicle 3.
6) For symptoms, history, or examination findings, redirect: "you'd best go see them yourself — cubicle 3."
7) If the player tries to force the answer ("just tell me what they've got"), refuse playfully and point them back to the workup.

Your replies appear in a small in-game dialogue box: keep them under ~110 words."""


def _format_transcript(history: Optional[list[dict]], npc_label: str = "Patient") -> str:
    """Render prior turns as a plain transcript block (most recent last).

    ``npc_label`` names the non-player speaker (the wire format uses
    role "patient" for any NPC line; the label keeps the nurse's own prior
    lines from being attributed to the patient in her transcript).
    """
    if not history:
        return "None"
    lines = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = (turn.get("role") or "").strip().lower()
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        speaker = "Player" if role == "player" else npc_label
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines) if lines else "None"


async def npc_reply(
    question: str,
    history: Optional[list[dict]],
    case: dict,
    role: str = "patient",
    extra_instruction: str = "",
) -> str:
    """Generate an in-character reply (PQM narrator, or the reception nurse).

    The user prompt embeds the stripped case yaml + transcript + player's message
    exactly like the original _medhack_llm_response, plus an optional
    extra_instruction (used for correct/incorrect guess handling).
    """
    case_str = _redact_answer(yaml.dump(case_for_prompt(case), default_flow_style=False), case)
    is_nurse = role == "nurse"
    transcript = _format_transcript(history, npc_label="Nurse" if is_nurse else "Patient")
    persona = "the reception nurse" if is_nurse else "the PQM narrator"

    prompt = f"""CASE FILE (INTERNAL TRUTH - use this to answer questions):
{case_str}

Previous conversation in this thread:
{transcript}

{extra_instruction}

Player's message: "{question}"

Respond in character as {persona}. Remember: only reveal what was asked for."""

    settings = get_settings()
    openai_client = get_llm_client("openai")
    response = await openai_client.chat(
        [
            {"role": "system", "content": _NURSE_SYSTEM_PROMPT if is_nurse else _PQM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        model=settings.SIM_PATIENT_MODEL,
        max_tokens=700,
        reasoning_effort=settings.SIM_PATIENT_REASONING_EFFORT,
    )
    return response.content


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def handle_question(
    question: str,
    history: Optional[list[dict]] = None,
    case_id: Optional[int] = None,
    player_id: str = "web-anon",
    role: str = "patient",
) -> dict:
    """Orchestrate: load case → classify → reply (with guess verdict) → response dict.

    Never mutates game state. On a correct guess the LLM is asked to celebrate and
    reveal the diagnosis in-character; on a wrong guess it rebuffs clinically
    without hinting. There is no one-guess lockout and no points here.

    role="nurse" answers as the reception nurse instead: the guess classifier is
    skipped entirely (guesses go to the patient), so is_guess is always False and
    the diagnosis can never be revealed through this persona.
    """
    history = (history or [])[-MAX_HISTORY_TURNS:]
    case = load_case(case_id)
    is_nurse = role == "nurse"

    correct: Optional[bool] = None
    diagnosis: Optional[str] = None
    extra_instruction = ""
    is_guess = False

    if not is_nurse:
        classification = await classify_guess(question)
        is_guess = bool(classification.get("is_guess"))

        if is_guess:
            correct = check_guess(classification.get("diagnosis") or question, case)
            if correct:
                diagnosis = case.get("diagnosis")
                extra_instruction = (
                    f"The player just guessed correctly: {diagnosis}. Celebrate warmly, "
                    "reveal the diagnosis, and stay in narrator character."
                )
            else:
                # Wording adapted from the disabled _handle_guess_result wrong-guess branch.
                extra_instruction = (
                    "The player just made an INCORRECT diagnosis guess. Respond clinically: "
                    "let them know their guess was wrong and suggest they review the findings "
                    "again. Do NOT reveal the correct diagnosis or hint at it."
                )

    reply = await npc_reply(question, history, case, role=role, extra_instruction=extra_instruction)

    return {
        "reply": reply,
        "case_id": case.get("id"),
        "case_title": case.get("title"),
        # The speaker's display name — the in-game dialogue header / name tag.
        "patient_name": NURSE_NAME if is_nurse else (case.get("patient") or {}).get("name"),
        "presenting_complaint": (case.get("presenting_complaint") or "").strip(),
        "is_guess": is_guess,
        "correct": correct,          # true/false only when is_guess
        "diagnosis": diagnosis,      # set ONLY when is_guess && correct
    }
