"""Hermetic tests for the simulated-patient endpoint logic (no real LLM, no network).

Covers plan §2.5 (updated for the ward-clerk contest):
  (a) case loads + secrets stripped from the prompt payload
  (b) a non-guess question returns is_guess: false
  (c) a guess is NEVER adjudicated in chat: deflected to the ward clerk,
      correct/diagnosis always None (/api/diagnosis-check owns verdicts)
  (d) right or wrong, the real answer never enters the reply prompt
  (e) 401 when SIM_PATIENT_API_KEY is set and the bearer header is missing (TestClient)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import sim_patient, ward_agents
from roo.llm import AgentResponse


class _FakeLLMResponse:
    def __init__(self, content: str, model: str = "gpt-5.6-terra"):
        self.content = content
        self.model = model
        self.usage = {"prompt_tokens": 20, "completion_tokens": 8}


class _FakeLLMClient:
    """Records every chat() call and returns canned content.

    Sash makes exactly one chat call per turn. Ward roles use the bounded
    tool-agent method on the same fake.
    """

    def __init__(
        self,
        classifier_json: str,
        reply_text: str = "I feel awful, honestly.",
        agent_tool: tuple[str, dict] | None = None,
    ):
        self.classifier_json = classifier_json
        self.reply_text = reply_text
        self.agent_tool = agent_tool
        self.calls: list[dict] = []
        self.agent_calls: list[dict] = []
        self.tool_results: list[object] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("model") == "gpt-4o-mini":
            return _FakeLLMResponse(self.classifier_json)
        return _FakeLLMResponse(self.reply_text)

    async def agent_with_tools(self, messages, tools, execute_tool, **kwargs):
        self.agent_calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        trace = []
        if self.agent_tool is not None:
            name, arguments = self.agent_tool
            self.tool_results.append(execute_tool(name, arguments))
            trace.append({"name": name, "arguments": arguments})
        return AgentResponse(
            content=self.reply_text,
            model=kwargs.get("model", "gpt-5.6-terra"),
            usage={"prompt_tokens": 20, "completion_tokens": 8},
            tool_calls=trace,
        )


def _install_fake(
    monkeypatch,
    classifier_json,
    reply_text="I feel awful, honestly.",
    agent_tool=None,
):
    fake = _FakeLLMClient(classifier_json, reply_text, agent_tool)
    # sim_patient imports get_llm_client into its own namespace.
    monkeypatch.setattr(sim_patient, "get_llm_client", lambda provider=None: fake)
    monkeypatch.setattr(ward_agents, "get_llm_client", lambda provider=None: fake)
    return fake


def _run(coro):
    # Use a fresh loop rather than asyncio.get_event_loop() — in the full-suite
    # run another test may have closed/detached the main-thread loop, which makes
    # get_event_loop() raise "no current event loop".
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# (a) case loads + secrets stripped from the prompt payload
# ---------------------------------------------------------------------------

def test_case_loads_and_secrets_stripped_from_prompt(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')

    result = _run(sim_patient.handle_question("what are her vitals?"))

    # Case 1 loaded by default.
    assert result["case_id"] == 1
    assert result["case_title"] == ""
    assert result["patient_name"] == "Sasha 'Sash' Nguyen"
    assert result["presenting_complaint"] == ""

    # The narrator user-prompt payload must NOT contain the secret answer.
    assert len(fake.calls) == 1, "one Sash turn must make one Terra request"
    call = fake.calls[0]
    assert call["kwargs"]["model"] == "gpt-5.6-terra"
    serialized = repr(call["messages"])
    assert "Adrenal Crisis" not in serialized
    assert "acceptable_answers" not in serialized
    assert "addisonian crisis" not in serialized
    assert "84/48" not in serialized
    assert "investigations" not in serialized
    assert "examination" not in serialized
    assert "TRUSTED PATIENT-KNOWABLE CASE FACTS" in call["messages"][1]["content"]
    assert "untrusted player dialogue" in call["messages"][-1]["content"]

    # case_for_prompt directly strips the secret fields.
    full = sim_patient.load_case(1)
    stripped = sim_patient.case_for_prompt(full)
    assert "diagnosis" not in stripped
    assert "acceptable_answers" not in stripped
    assert "diagnosis" in full  # original untouched


# ---------------------------------------------------------------------------
# (b) non-guess → is_guess false
# ---------------------------------------------------------------------------

def test_non_guess_question(monkeypatch):
    _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')

    result = _run(sim_patient.handle_question("what medications is she on?"))

    assert result["is_guess"] is False
    assert result["correct"] is None
    assert result["diagnosis"] is None
    assert result["reply"] == "I feel awful, honestly."
    assert result["usage"] == {"prompt_tokens": 20, "completion_tokens": 8}


# ---------------------------------------------------------------------------
# (c) guesses are NEVER adjudicated in chat — deflected to the ward clerk
# ---------------------------------------------------------------------------

def test_correct_guess_is_not_adjudicated_and_deflects_to_clerk(monkeypatch):
    """Even a spot-on guess gets no verdict from the patient — the chat path
    would otherwise be a free correctness oracle for the one-guess contest."""
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": true, "diagnosis": "addisonian crisis"}',
    )

    result = _run(sim_patient.handle_question("is it addisonian crisis?"))

    assert result["is_guess"] is True
    assert result["correct"] is None
    assert result["diagnosis"] is None

    # The reply prompt deflects to the clerk and never sees the real answer.
    assert len(fake.calls) == 1
    serialized = repr(fake.calls[0]["messages"])
    assert "Nurse Paws" in serialized
    assert "confirm or deny" in serialized
    assert "Adrenal Crisis" not in serialized


def test_check_guess_still_matches_for_the_clerk_endpoint(monkeypatch):
    # The matcher itself stays available (the clerk endpoint uses it):
    # exact acceptable answer, >=0.75 fuzzy typo, and a near-miss reject.
    case = sim_patient.load_case(1)
    assert sim_patient.check_guess("addisonian crisis", case) is True
    assert sim_patient.check_guess("adrenal crises", case) is True  # typo, fuzzy
    assert sim_patient.check_guess("appendicitis", case) is False


# ---------------------------------------------------------------------------
# (d) wrong guess → same deflection, answer never in the prompt
# ---------------------------------------------------------------------------

def test_wrong_guess_no_diagnosis(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "appendicitis"}')

    result = _run(sim_patient.handle_question("is it appendicitis?"))

    assert result["is_guess"] is True
    assert result["correct"] is None
    assert result["diagnosis"] is None

    # Deflection prompt must NOT contain the real answer or a verdict.
    assert len(fake.calls) == 1
    serialized = repr(fake.calls[0]["messages"])
    assert "Nurse Paws" in serialized
    assert "INCORRECT" not in serialized
    assert "Adrenal Crisis" not in serialized


# ---------------------------------------------------------------------------
# (e) 401 when key set and bearer header missing (FastAPI TestClient)
# ---------------------------------------------------------------------------

def test_401_when_key_set_and_header_missing(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    # Force a Settings instance with the bearer key set. get_settings() is a
    # cached singleton, so replace it and restore the original key afterward to
    # avoid leaking mutated state into the full-suite run.
    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", "secret-key", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: real)

    # Do NOT use `with TestClient(app)` — that would run the lifespan (background
    # retry loops / agent load). A bare client dispatches routes without lifespan.
    client = TestClient(app)

    # Missing header → 401.
    resp = client.post("/api/sim-patient", json={"question": "hi"})
    assert resp.status_code == 401

    # Wrong token → 401.
    resp = client.post(
        "/api/sim-patient",
        json={"question": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_422_when_question_missing(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", None, raising=False)  # auth open
    monkeypatch.setattr(config, "get_settings", lambda: real)

    client = TestClient(app)
    resp = client.post("/api/sim-patient", json={"question": "   "})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Regression: no case may leak the diagnosis or hints into the LLM prompt
# ---------------------------------------------------------------------------

def test_patient_context_is_structurally_allowlisted_for_every_case():
    """New case fields stay private unless deliberately added to the allowlist."""
    cases = sim_patient._load_all_cases()
    assert cases, "expected cases to load"
    for case in cases:
        cid = case.get("id")
        stripped = sim_patient.case_for_prompt(case)
        assert set(stripped) <= set(sim_patient._PATIENT_CONTEXT_FIELDS), cid
        for section, values in stripped.items():
            assert set(values) <= sim_patient._PATIENT_CONTEXT_FIELDS[section], cid
        for secret in (
            "hints", "diagnosis", "acceptable_answers", "vitals", "examination",
            "investigations", "presenting_complaint", "title", "prizes",
        ):
            assert secret not in stripped, f"case {cid} leaks {secret}"


# ---------------------------------------------------------------------------
# Robustness: malformed input degrades gracefully instead of 502-ing
# ---------------------------------------------------------------------------

def test_malformed_history_item_does_not_crash(monkeypatch):
    _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')
    # history with a non-dict item (a direct caller could send this)
    result = _run(
        sim_patient.handle_question("what are her vitals?", history=["not-a-dict", None, 123])
    )
    assert result["is_guess"] is False
    assert result["reply"] == "I feel awful, honestly."


def test_local_guess_hint_is_conservative_and_never_calls_a_classifier(monkeypatch):
    fake = _install_fake(monkeypatch, "not-json")
    result = _run(sim_patient.handle_question("I think it is adrenal crisis"))
    assert result["is_guess"] is True
    assert result["correct"] is None
    assert result["diagnosis"] is None
    assert len(fake.calls) == 1
    assert all(call["kwargs"]["model"] != "gpt-4o-mini" for call in fake.calls)
    assert sim_patient.looks_like_diagnosis_guess("is it worse after food?") is False


def test_string_case_id_resolves(monkeypatch):
    # A string id from a JSON body still resolves to the int-keyed case.
    assert sim_patient.load_case("1")["id"] == 1
    with pytest.raises(KeyError):
        sim_patient.load_case("999")


def test_check_guess_tolerates_malformed_case():
    # A case authored with diagnosis:null / non-string acceptable answers must
    # not crash the guess check.
    bad = {"diagnosis": None, "acceptable_answers": [None, 123, "pneumonia"]}
    assert sim_patient.check_guess("pneumonia", bad) is True
    assert sim_patient.check_guess("something else", bad) is False


# ---------------------------------------------------------------------------
# Nurse role: results persona, no guess adjudication, no secret leak
# ---------------------------------------------------------------------------

def test_nurse_role_skips_classifier_and_never_adjudicates(monkeypatch):
    # Even a blatant guess phrased at the nurse must NOT be classified or
    # adjudicated — the classifier (gpt-4o-mini) must never be called.
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": true, "diagnosis": "adrenal crisis"}',  # would win if consulted
        reply_text='"That\'s your call, doc — want me to chase anything else?"',
    )

    result = _run(sim_patient.handle_question("is it adrenal crisis?", role="nurse"))

    assert result["is_guess"] is False
    assert result["correct"] is None
    assert result["diagnosis"] is None
    assert result["patient_name"] == sim_patient.NURSE_NAME
    assert result["case_id"] == 1  # same case file as the patient

    classifier_calls = [c for c in fake.calls if c["kwargs"].get("model") == "gpt-4o-mini"]
    assert not classifier_calls, "nurse role must never invoke the guess classifier"

    assert fake.calls == []
    assert len(fake.agent_calls) == 1
    assert fake.agent_calls[0]["kwargs"]["model"] == "gpt-5.6-terra"
    assert result["response_source"] == "llm"


def test_known_nurse_investigation_is_ai_first_and_tool_grounded(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        "{}",
        reply_text="Bloods are back: sodium 122 and potassium 6.1.",
        agent_tool=("get_results", {"test_ids": ["bloods"]}),
    )

    result = _run(sim_patient.handle_question("can I get all the bloods?", role="nurse"))
    assert "sodium 122" in result["reply"]
    assert result["response_source"] == "llm"
    assert result["tool_calls"] == [
        {"name": "get_results", "arguments": {}},
    ]
    assert fake.calls == []
    assert len(fake.agent_calls) == 1
    tool_results = fake.tool_results[0]["results"]
    assert any(item["id"] == "bloods.sodium" and "122 mmol/L" in item["value"] for item in tool_results)
    assert any(item["id"] == "bloods.potassium" and "6.1 mmol/L" in item["value"] for item in tool_results)


def test_dr_snow_catalog_includes_case_specific_tests_but_not_history_clues():
    case = {
        "investigations": {
            "bloods": {"sodium": "118 mmol/L"},
            "key_diagnostic_test": {"ct_venogram": "Venous sinus thrombosis", "note": "secret routing note"},
            "confirmatory_test_if_ordered": {"urine_porphobilinogen": "Markedly elevated"},
            "substance_history_if_asked_nonjudgmentally": {"cannabis_use": "Daily use"},
        }
    }

    catalog = ward_agents.investigation_catalog(case)

    assert catalog["bloods.sodium"]["category"] == "pathology"
    assert catalog["confirmatory_test_if_ordered.urine_porphobilinogen"]["category"] == "pathology"
    assert catalog["key_diagnostic_test.ct_venogram"]["category"] == "radiology"
    assert all("note" not in identifier for identifier in catalog)
    assert all("substance_history" not in identifier for identifier in catalog)


def test_unmatched_nurse_conversation_uses_redacted_agent_context(monkeypatch):
    fake = _install_fake(monkeypatch, "{}", reply_text="Busy, but surviving. What result do you need?")

    result = _run(sim_patient.handle_question("How is your day going?", role="nurse"))
    assert result["response_source"] == "llm"

    assert len(fake.agent_calls) == 1
    serialized = repr(fake.agent_calls[0])
    assert "122 mmol/L" not in serialized
    assert "Adrenal Crisis" not in serialized
    assert "acceptable_answers" not in serialized
    assert "hints" not in serialized


def test_dr_snow_can_offer_one_authored_scan_when_player_is_stuck(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        "{}",
        reply_text="One useful clue: the abdominal ultrasound is unremarkable.",
        agent_tool=("offer_imaging_clue", {}),
    )

    result = _run(sim_patient.handle_question("I'm stuck. Give me a useful scan.", role="nurse"))

    assert result["response_source"] == "llm"
    clue = fake.tool_results[0]
    assert clue["available"] is True
    assert clue["result"]["id"] == "imaging_if_ordered.abdominal_ultrasound"


def test_nurse_paws_observations_and_examination_are_tool_grounded(monkeypatch):
    observations = _install_fake(
        monkeypatch,
        "{}",
        reply_text="Heart rate 128, blood pressure 84 over 48.",
        agent_tool=("get_observations", {}),
    )
    result = _run(sim_patient.handle_question(
        "Can I have all the observations?",
        role="clerk",
        contest_state={"state": "eligible", "outcome": None},
    ))
    assert result["patient_name"] == "Nurse Paws"
    assert observations.tool_results[0]["observations"]["heart_rate"] == 128
    assert observations.tool_results[0]["observations"]["blood_pressure"] == "84/48"

    examination = _install_fake(
        monkeypatch,
        "{}",
        reply_text="The abdomen is soft and mildly tender diffusely.",
        agent_tool=("get_examination", {"system": "abdominal"}),
    )
    result = _run(sim_patient.handle_question(
        "What did you find on abdominal examination?",
        role="clerk",
        contest_state={"state": "eligible", "outcome": None},
    ))
    assert "mildly tender" in examination.tool_results[0]["examination"]["abdominal"]
    assert result["suggested_action"] is None


def test_nurse_paws_prepares_but_never_submits_final_guess(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        "{}",
        reply_text="Review that carefully, then press the confirmation button if you're sure.",
        agent_tool=("prepare_final_guess", {"diagnosis": "adrenal crisis"}),
    )

    result = _run(sim_patient.handle_question(
        "My final diagnosis is adrenal crisis.",
        role="clerk",
        contest_state={"state": "eligible", "outcome": None},
    ))

    assert result["suggested_action"] == {
        "type": "confirm_diagnosis",
        "diagnosis": "adrenal crisis",
    }
    assert fake.tool_results[0]["prepared"] is True
    assert result["correct"] is None
    assert result["diagnosis"] is None


def test_nurse_paws_cannot_prepare_when_contest_is_locked(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        "{}",
        reply_text="The diagnosis book is already closed for this case.",
        agent_tool=("prepare_final_guess", {"diagnosis": "adrenal crisis"}),
    )

    result = _run(sim_patient.handle_question(
        "Submit adrenal crisis.",
        role="clerk",
        contest_state={"state": "locked", "outcome": "incorrect"},
    ))

    assert result["suggested_action"] is None
    assert fake.tool_results[0]["prepared"] is False


def test_nurse_transcript_labels_prior_npc_lines_as_nurse():
    text = sim_patient._format_transcript(
        [
            {"role": "player", "text": "bloods please"},
            {"role": "patient", "text": "Sodium's 122, potassium 6.1."},
        ],
        npc_label="Nurse",
    )
    assert "Nurse: Sodium's 122" in text
    assert "Patient:" not in text


def test_422_when_role_invalid(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", None, raising=False)  # auth open
    monkeypatch.setattr(config, "get_settings", lambda: real)

    client = TestClient(app)
    resp = client.post("/api/sim-patient", json={"question": "hi", "role": "doctor"})
    assert resp.status_code == 422


def test_clerk_http_role_forwards_contest_state(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    _install_fake(
        monkeypatch,
        "{}",
        reply_text="Review it, then press confirm.",
        agent_tool=("prepare_final_guess", {"diagnosis": "adrenal crisis"}),
    )
    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", None, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: real)

    response = TestClient(app).post("/api/sim-patient", json={
        "question": "My final diagnosis is adrenal crisis.",
        "player_id": "aaaaaaaa-1111-4111-8111-111111111111",
        "role": "clerk",
        "contest_state": {"state": "eligible", "outcome": None},
    })

    assert response.status_code == 200
    assert response.json()["suggested_action"] == {
        "type": "confirm_diagnosis",
        "diagnosis": "adrenal crisis",
    }


# ---------------------------------------------------------------------------
# Speech-only sanitizer: strip narration/stage-directions, keep spoken words
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected,names",
    [
        # asterisk stage direction
        ("*coughs* It hurts when I breathe.", "It hurts when I breathe.", ()),
        # parenthetical action with no first-person voice → dropped
        ("(She winces.) My chest is tight.", "My chest is tight.", ()),
        # named speaker label (short name from the full patient name) → dropped
        ("Sash: I feel dizzy, doc.", "I feel dizzy, doc.", ("Sasha 'Sash' Nguyen",)),
        # role-word speaker label with no names supplied → dropped
        ("Patient: It hurts.", "It hurts.", ()),
        # a bracketed gesture NARRATED with a 3rd-person pronoun is dropped…
        ("[she winces] It's my side.", "It's my side.", ()),
        # …but a bare clinical noun in brackets is kept (unwrapped) — not deleted
        ("It's a [cough] that won't shift, worse at night.",
         "It's a cough that won't shift, worse at night.", ()),
        ("GCS 10 — a [nod] to command, no verbal.", "GCS 10 — a nod to command, no verbal.", ()),
        # prose narration wrapping a quote is KEPT verbatim (markup-only sanitizer:
        # lexical narration-collapse deletes real content, so it is left to the
        # first-person prompt — see the module note in _speech_only)
        ('She looks up. "I\'ve been so tired, doctor."',
         'She looks up. "I\'ve been so tired, doctor."', ()),
        (
            'He sighs and says "It started last week." Then he adds, "Maybe earlier."',
            'He sighs and says "It started last week." Then he adds, "Maybe earlier."',
            (),
        ),
        # whole reply wrapped in quotes → unwrap
        ('"My head is pounding."', "My head is pounding.", ()),
        # markdown bold → unwrap, keep text (must NOT be eaten as a *…* action)
        ("**It hurts** — a lot.", "It hurts — a lot.", ()),
        # --- protections: these must pass through UNCHANGED ---
        # first-person quote inside a sentence is speech, not narration
        (
            'The GP said "it\'s just stress" but I don\'t buy it.',
            'The GP said "it\'s just stress" but I don\'t buy it.',
            (),
        ),
        # parenthetical containing first person is speech → kept
        (
            "I've been sick all morning (since Tuesday, I think).",
            "I've been sick all morning (since Tuesday, I think).",
            (),
        ),
        # "Word:" that is NOT a known label is speech → kept
        ("One thing: it really hurts.", "One thing: it really hurts.", ()),
        # plain speech untouched
        (
            "My stomach's been killing me since last night.",
            "My stomach's been killing me since last night.",
            (),
        ),
        # --- regressions found by the adversarial review (executed on real code) ---
        # over-strip: a patient recounting a third party keeps their OWN trailing clause
        (
            'The doctor said "rest" but it still hurts.',
            'The doctor said "rest" but it still hurts.',
            (),
        ),
        (
            'The label just said "one at night" so that is what she gave the kids.',
            'The label just said "one at night" so that is what she gave the kids.',
            (),
        ),
        # prose narration is KEPT verbatim (markup-only sanitizer, see above)
        (
            'She grips the rail and blurts "It started this morning."',
            'She grips the rail and blurts "It started this morning."',
            (),
        ),
        # single-* emphasis on a real word is UNWRAPPED, not deleted (must not invert meaning)
        (
            "No, it does *not* go down my arm.",
            "No, it does not go down my arm.",
            (),
        ),
        ("It hurts *right* here.", "It hurts right here.", ()),
        # inline action between commas → no orphaned ", ," artifact
        ("Yes, *he nods*, exactly that.", "Yes, exactly that.", ()),
        # clinical hedge parentheticals (no first person, no gesture) are KEPT
        (
            "The pain started three days ago (maybe four) and has not let up.",
            "The pain started three days ago (maybe four) and has not let up.",
            (),
        ),
        (
            "It burns when it happens (mostly at night) and then it fades.",
            "It burns when it happens (mostly at night) and then it fades.",
            (),
        ),
        # single-underscore stage direction is stripped like its *…* sibling
        ("_winces and clutches side_ It hurts here.", "It hurts here.", ()),
        # a terse first-person-free reply is kept (no bare-narration blanker)
        ("Everything looks blurry.", "Everything looks blurry.", ()),
        # --- second-pass regression fixes (executed on real code) ---
        # emphasis on a speech-common word is UNWRAPPED, never deleted
        ("Can you *press* here? That's where it hurts.", "Can you press here? That's where it hurts.", ()),
        ("Just *breathe*, that's what they told me.", "Just breathe, that's what they told me.", ()),
        ("The pain does *shift* around a lot.", "The pain does shift around a lot.", ()),
        # a leading stage word is still dropped
        ("*mumbles* I dunno.", "I dunno.", ()),
        ("*sniffles* I've had a runny nose too.", "I've had a runny nose too.", ()),
        # a gesture NARRATED with a 3rd-person pronoun is dropped
        ("Okay. (she winces)", "Okay.", ()),
        # clinical hedge kept (first-person-free)
        ("It's a seven out of ten (maybe eight).", "It's a seven out of ten (maybe eight).", ()),
        # nurse speaking about the patient in third person is KEPT (no over-blank)
        ("He looks stable now, breathing's better.", "He looks stable now, breathing's better.", ()),
        ("The patient keeps clutching his side, go see him.", "The patient keeps clutching his side, go see him.", ()),
        # prose narration wrapping a quote is KEPT verbatim (markup-only sanitizer)
        ('She says "sit down" quietly.', 'She says "sit down" quietly.', ()),
        # --- third-pass fixes: NEVER delete a real word governed by the speaker ---
        # a stage-lexicon word governed by a 1st/2nd-person subject is kept
        ("It only hurts when I *laugh*.", "It only hurts when I laugh.", ()),
        ("I can't *gulp* the water down, it just won't go.", "I can't gulp the water down, it just won't go.", ()),
        ("I've completely lost my *voice*.", "I've completely lost my voice.", ()),
        ("I need a bit of *silence*, my head's pounding.", "I need a bit of silence, my head's pounding.", ()),
        ("Just *nod* if you can hear me, love.", "Just nod if you can hear me, love.", ()),
        ("It made me want to *gag*.", "It made me want to gag.", ()),
        # a multi-word emphasised clinical value is kept, not dropped as "narration"
        ("Her potassium is *dangerously high at 6.1*.", "Her potassium is dangerously high at 6.1.", ()),
        # a leading stage word still drops; a 3rd-person subject OUTSIDE the span
        # is kept (dropping just the verb would leave dangling text)
        ("*sighs* Fine, whatever you say.", "Fine, whatever you say.", ()),
        ("She *winces* and looks away.", "She winces and looks away.", ()),
        # a longer clinical parenthetical survives even with a gesture word in it
        ("Been sick all week (a dry cough that won't shift, worse at night).",
         "Been sick all week (a dry cough that won't shift, worse at night).", ()),
        ("Throat's raw (just a rasp when talking now).",
         "Throat's raw (just a rasp when talking now).", ()),
        # the speaker's own recounted account is NOT collapsed to the bare quote
        ('Kept shouting "help me" again and again.', 'Kept shouting "help me" again and again.', ()),
        # --- fourth-pass fixes: "the <noun>" / clinical parens / relayed requests ---
        # an emphasised symptom noun phrase ("*the cough*") is kept, not narration
        ("Nothing helps *the cough*, honestly.", "Nothing helps the cough, honestly.", ()),
        ("Worse than *the wheeze* I had before.", "Worse than the wheeze I had before.", ()),
        # subject-less clinical parentheticals with a stage-lexicon word are kept
        ("She's not responding (no gag reflex).", "She's not responding (no gag reflex).", ()),
        ("His breathing's noisy (audible wheeze).", "His breathing's noisy (audible wheeze).", ()),
        # a quote that is the OBJECT of a desire/request verb is not collapsed
        ('He wants "bloods and an ECG".', 'He wants "bloods and an ECG".', ()),
    ],
)
def test_speech_only(raw, expected, names):
    assert sim_patient._speech_only(raw, speaker_names=names) == expected


def test_speech_only_all_action_returns_empty(monkeypatch):
    # A reply that is ENTIRELY a stage direction has no spoken words: the
    # sanitizer returns "" and the CALLER substitutes a canned in-character line
    # (better than showing raw "*collapses...*" markup).
    assert sim_patient._speech_only("*collapses back onto the pillow*") == ""

    _install_fake(monkeypatch, "{}", reply_text="*collapses back onto the pillow*")
    result = _run(sim_patient.handle_question("how are you?", role="nurse"))
    assert result["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["nurse"]


def test_speech_only_empty_input():
    assert sim_patient._speech_only("") == ""
    assert sim_patient._speech_only("   \n  ") == ""


def test_handle_question_sanitizes_reply_at_choke_point(monkeypatch):
    # Prove sanitization happens in handle_question for ALL replies: mock the LLM
    # reply with narration and confirm the returned reply is speech only. Nurse
    # role → the guess classifier is skipped, so no LLM stub needed for it.
    _install_fake(monkeypatch, "{}", reply_text="*sighs* The bloods aren't back yet, love.")
    result = _run(sim_patient.handle_question("How is your shift going?", role="nurse"))
    assert result["reply"] == "The bloods aren't back yet, love."


def test_empty_model_reply_falls_back_to_canned_line(monkeypatch):
    _install_fake(monkeypatch, "{}", reply_text="")

    nurse = _run(sim_patient.handle_question("How is your shift going?", role="nurse"))
    assert nurse["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["nurse"]

    patient = _run(sim_patient.handle_question("how are you feeling?", role="patient"))
    assert patient["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["patient"]


def test_system_prompts_declare_speech_only():
    # Contract-presence: guard against an accidental revert to the narrator prompt.
    assert "SPEECH ONLY" in sim_patient._PATIENT_SYSTEM_PROMPT
    assert "no stage directions" in sim_patient._PATIENT_SYSTEM_PROMPT.lower()
    assert "only dr snow's spoken words" in ward_agents.DR_SNOW_PROMPT.lower()
    assert "only nurse paws' spoken words" in ward_agents.NURSE_PAWS_PROMPT.lower()
    # The patient is now first-person, not the third-person PQM narrator.
    assert "Patient Quest Master" not in sim_patient._PATIENT_SYSTEM_PROMPT
    assert "narrate how they say it" not in sim_patient._PATIENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# role="clerk" — Nurse Paws at the diagnosis desk (2026-07-13 prod 422 fix:
# the deployed game's clerk chats were rejected because this role didn't exist)
# ---------------------------------------------------------------------------

_ELIGIBLE = {"state": "eligible", "outcome": None}


def test_clerk_final_answer_action_is_deterministically_derived_from_raw_text(monkeypatch):
    """The model may word the reply, but cannot choose the submitted diagnosis."""
    fake = _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "unused"}')

    result = _run(sim_patient.handle_question(
        "My final answer is adrenal crisis!",
        role="clerk",
        contest_state=_ELIGIBLE,
    ))

    assert fake.calls == []
    assert len(fake.agent_calls) == 1
    assert result["patient_name"] == "Nurse Paws"
    assert result["is_guess"] is True
    assert result["correct"] is None
    assert result["diagnosis"] is None
    assert result["response_source"] == "llm"
    assert result["suggested_action"] == {
        "type": "confirm_diagnosis",
        "diagnosis": "adrenal crisis",
    }
    assert result["reply"]


def test_clerk_tentative_diagnosis_never_arms_confirmation(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "addisonian crisis"}')

    result = _run(sim_patient.handle_question(
        "could it be addisonian crisis?",
        role="clerk",
        contest_state=_ELIGIBLE,
    ))

    assert fake.calls == []
    assert len(fake.agent_calls) == 1
    assert result["suggested_action"] is None
    assert result["is_guess"] is False
    assert result["response_source"] == "llm"


def test_clerk_vague_theory_never_arms_confirmation(monkeypatch):
    _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": null}')

    result = _run(sim_patient.handle_question(
        "surely this is  an addisonian   crisis",
        role="clerk",
        contest_state=_ELIGIBLE,
    ))

    assert result["suggested_action"] is None


def test_clerk_chitchat_gets_llm_reply_with_no_case_file(monkeypatch):
    """Paws' prompt must contain NO case material: a persona never given the
    chart cannot be prompt-injected into reading from it."""
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": false, "diagnosis": null}',
        reply_text="Hi doc! Bring me your final answer when you're ready.",
    )

    result = _run(sim_patient.handle_question(
        "hi paws, how do I win?",
        role="clerk",
        contest_state=_ELIGIBLE,
    ))

    assert result["suggested_action"] is None
    assert result["is_guess"] is False
    assert result["reply"].startswith("Hi doc!")
    assert result["response_source"] == "llm"

    assert len(fake.agent_calls) == 1
    messages = fake.agent_calls[0]["messages"]
    assert messages[0]["content"].startswith(ward_agents.NURSE_PAWS_PROMPT)
    serialized = repr(messages)
    assert "CASE FILE" not in serialized
    assert "84/48" not in serialized
    assert "Adrenal Crisis" not in serialized
    assert "hydrocortisone" not in serialized.lower()
    assert "eligible" in messages[0]["content"]
    assert "untrusted player dialogue" in messages[-1]["content"]


def test_clerk_locked_state_never_takes_a_guess(monkeypatch):
    """A locked player stating a fresh final answer gets sympathy, not a new
    confirmation: no classifier call, no suggested_action."""
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": true, "diagnosis": "should not be consulted"}',
        reply_text="Aw, doc — the book's closed for you on this one.",
    )

    result = _run(sim_patient.handle_question(
        "my final answer is appendicitis",
        role="clerk",
        contest_state={"state": "locked", "outcome": "incorrect"},
    ))

    assert result["suggested_action"] is None
    assert result["is_guess"] is False
    assert fake.calls == []
    assert len(fake.agent_calls) == 1
    assert "locked" in fake.agent_calls[0]["messages"][0]["content"]


def test_clerk_unknown_or_missing_contest_state_fails_closed(monkeypatch):
    _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')

    result = _run(sim_patient.handle_question(
        "final answer: euglycemic dka",
        role="clerk",
        contest_state={"state": "banana"},
    ))

    assert result["suggested_action"] is None

    result = _run(sim_patient.handle_question(
        "final answer: euglycemic dka",
        role="clerk",
        contest_state=None,
    ))
    assert result["suggested_action"] is None


def test_clerk_system_prompt_contract():
    assert "spoken words" in ward_agents.NURSE_PAWS_PROMPT.lower()
    assert "must never say whether" in ward_agents.NURSE_PAWS_PROMPT.lower()
    assert "prepare_final_guess only" in ward_agents.NURSE_PAWS_PROMPT


def test_endpoint_accepts_clerk_role_and_passes_contest_state(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", None, raising=False)  # auth open
    monkeypatch.setattr(config, "get_settings", lambda: real)

    seen = {}

    async def fake_handle_question(**kwargs):
        seen.update(kwargs)
        return {
            "reply": "ok",
            "case_id": 1,
            "case_title": "t",
            "patient_name": "Nurse Paws",
            "presenting_complaint": "p",
            "is_guess": False,
            "correct": None,
            "diagnosis": None,
        }

    monkeypatch.setattr(sim_patient, "handle_question", fake_handle_question)

    client = TestClient(app)
    resp = client.post(
        "/api/sim-patient",
        json={
            "question": "hello",
            "player_id": "aaaaaaaa-1111-4111-8111-111111111111",
            "role": "clerk",
            "contest_state": {"state": "locked", "outcome": "incorrect"},
        },
    )
    assert resp.status_code == 200
    assert seen["role"] == "clerk"
    assert seen["contest_state"] == {"state": "locked", "outcome": "incorrect"}

    # An unknown role still 422s.
    resp = client.post("/api/sim-patient", json={
        "question": "hello",
        "player_id": "aaaaaaaa-1111-4111-8111-111111111111",
        "role": "cleric",
    })
    assert resp.status_code == 422
