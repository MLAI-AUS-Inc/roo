"""Hermetic tests for the simulated-patient endpoint logic (no real LLM, no network).

Covers plan §2.5:
  (a) case loads + secrets stripped from the prompt payload
  (b) a non-guess question returns is_guess: false
  (c) a guess matching acceptable_answers/fuzzy diagnosis returns correct: true + diagnosis
  (d) a wrong guess returns correct: false and no diagnosis
  (e) 401 when SIM_PATIENT_API_KEY is set and the bearer header is missing (TestClient)
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import sim_patient


class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLMClient:
    """Records every chat() call and returns canned content.

    Both the guess classifier (gpt-4o-mini) and the narrator reply route through
    get_llm_client("openai").chat(...). We branch on the model kwarg so a single
    fake serves both: gpt-4o-mini → the classifier JSON, anything else → reply.
    """

    def __init__(self, classifier_json: str, reply_text: str = "I feel awful, honestly."):
        self.classifier_json = classifier_json
        self.reply_text = reply_text
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("model") == "gpt-4o-mini":
            return _FakeLLMResponse(self.classifier_json)
        return _FakeLLMResponse(self.reply_text)


def _install_fake(monkeypatch, classifier_json, reply_text="I feel awful, honestly."):
    fake = _FakeLLMClient(classifier_json, reply_text)
    # sim_patient imports get_llm_client into its own namespace.
    monkeypatch.setattr(sim_patient, "get_llm_client", lambda provider=None: fake)
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
    assert result["case_title"] == "Salt & Static"
    assert result["patient_name"] == "Sasha 'Sash' Nguyen"
    assert result["presenting_complaint"].startswith("27F")

    # The narrator user-prompt payload must NOT contain the secret answer.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    assert reply_calls, "expected a narrator reply LLM call"
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "Adrenal Crisis" not in user_prompt
    assert "acceptable_answers" not in user_prompt
    assert "addisonian crisis" not in user_prompt
    # But the case content (vitals etc.) IS present.
    assert "CASE FILE" in user_prompt

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


# ---------------------------------------------------------------------------
# (c) correct guess (via acceptable_answers / fuzzy) → correct true + diagnosis
# ---------------------------------------------------------------------------

def test_correct_guess_reveals_diagnosis(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": true, "diagnosis": "addisonian crisis"}',
        reply_text="Sash grins weakly. \"Yeah… that's it.\"",
    )

    result = _run(sim_patient.handle_question("is it addisonian crisis?"))

    assert result["is_guess"] is True
    assert result["correct"] is True
    assert result["diagnosis"] == "Adrenal Crisis"

    # The reply prompt should carry the celebratory extra instruction with the dx.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "guessed correctly" in user_prompt
    assert "Adrenal Crisis" in user_prompt  # revealed to the LLM only on a correct guess


def test_correct_guess_via_fuzzy_match(monkeypatch):
    # "adrenal crises" (typo) fuzzy-matches "adrenal crisis" at >= 0.75.
    _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "adrenal crises"}')

    result = _run(sim_patient.handle_question("adrenal crises?"))

    assert result["is_guess"] is True
    assert result["correct"] is True
    assert result["diagnosis"] == "Adrenal Crisis"


# ---------------------------------------------------------------------------
# (d) wrong guess → correct false, no diagnosis
# ---------------------------------------------------------------------------

def test_wrong_guess_no_diagnosis(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "appendicitis"}')

    result = _run(sim_patient.handle_question("is it appendicitis?"))

    assert result["is_guess"] is True
    assert result["correct"] is False
    assert result["diagnosis"] is None

    # Wrong-guess reply prompt must NOT contain the real answer.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "INCORRECT" in user_prompt
    assert "Adrenal Crisis" not in user_prompt


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

def test_no_case_leaks_diagnosis_or_hints_into_prompt():
    """For EVERY case in cases.yaml, the case text embedded in the LLM prompt
    (case_for_prompt → yaml → _redact_answer) must NOT contain the primary
    diagnosis string, the secret keys, or the authored hints. Guards against
    the deny-list gap where notes/hints named the answer verbatim."""
    import yaml as _yaml

    cases = sim_patient._load_all_cases()
    assert cases, "expected cases to load"
    for case in cases:
        cid = case.get("id")
        stripped = sim_patient.case_for_prompt(case)
        assert "hints" not in stripped, f"case {cid} leaks hints key"
        assert "diagnosis" not in stripped, f"case {cid} leaks diagnosis key"
        assert "acceptable_answers" not in stripped, f"case {cid} leaks acceptable_answers key"

        prompt_text = sim_patient._redact_answer(
            _yaml.dump(stripped, default_flow_style=False), case
        )
        dx = case["diagnosis"]
        assert dx.lower() not in prompt_text.lower(), (
            f"case {cid} leaks primary diagnosis '{dx}' into the prompt"
        )
        # hint free-text must be gone too
        for hint in case.get("hints", []):
            assert str(hint) not in prompt_text, f"case {cid} leaks a hint into the prompt"


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


def test_non_string_classifier_diagnosis_does_not_crash(monkeypatch):
    # classifier returns a truthy NON-string diagnosis (array) — must not crash.
    _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": ["adrenal crisis"]}')
    result = _run(sim_patient.handle_question("is it adrenal crisis?"))
    assert result["is_guess"] is True
    # diagnosis coerced to None → check_guess falls back to the raw question,
    # which fuzzy-matches "adrenal crisis" → correct.
    assert result["correct"] is True
    assert result["diagnosis"] == "Adrenal Crisis"


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

    # The reply call uses the nurse persona, not the PQM narrator.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    assert len(reply_calls) == 1
    system_prompt = reply_calls[0]["messages"][0]["content"]
    assert "Nurse Priya" in system_prompt
    assert "Patient Quest Master" not in system_prompt


def test_nurse_prompt_carries_case_but_no_secrets(monkeypatch):
    fake = _install_fake(monkeypatch, "{}", reply_text='"Bloods are back: sodium 122."')

    result = _run(sim_patient.handle_question("can I get the bloods?", role="nurse"))
    # The speech-only sanitizer unwraps the model's wrapping quotes.
    assert result["reply"] == "Bloods are back: sodium 122."

    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    user_prompt = reply_calls[0]["messages"][1]["content"]
    # Case content present (so she can actually read results out)…
    assert "CASE FILE" in user_prompt
    assert "122 mmol/L" in user_prompt
    # …but never the secret answer, keys, or hints.
    assert "Adrenal Crisis" not in user_prompt
    assert "acceptable_answers" not in user_prompt
    assert "addisonian" not in user_prompt.lower()
    assert "hints" not in user_prompt


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

    async def action_only(*args, **kwargs):
        return "*collapses back onto the pillow*"

    monkeypatch.setattr(sim_patient, "npc_reply", action_only)
    result = _run(sim_patient.handle_question("how are you?", role="nurse"))
    assert result["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["nurse"]


def test_speech_only_empty_input():
    assert sim_patient._speech_only("") == ""
    assert sim_patient._speech_only("   \n  ") == ""


def test_handle_question_sanitizes_reply_at_choke_point(monkeypatch):
    # Prove sanitization happens in handle_question for ALL replies: mock the LLM
    # reply with narration and confirm the returned reply is speech only. Nurse
    # role → the guess classifier is skipped, so no LLM stub needed for it.
    async def fake_npc_reply(*args, **kwargs):
        return "*sighs* The bloods aren't back yet, love."

    monkeypatch.setattr(sim_patient, "npc_reply", fake_npc_reply)
    result = _run(sim_patient.handle_question("bloods?", role="nurse"))
    assert result["reply"] == "The bloods aren't back yet, love."


def test_empty_model_reply_falls_back_to_canned_line(monkeypatch):
    async def empty_reply(*args, **kwargs):
        return ""

    monkeypatch.setattr(sim_patient, "npc_reply", empty_reply)

    nurse = _run(sim_patient.handle_question("bloods?", role="nurse"))
    assert nurse["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["nurse"]

    # Patient role runs the classifier first — stub it to a non-guess so no LLM.
    async def not_a_guess(_question):
        return {"is_guess": False, "diagnosis": None}

    monkeypatch.setattr(sim_patient, "classify_guess", not_a_guess)
    patient = _run(sim_patient.handle_question("how are you feeling?", role="patient"))
    assert patient["reply"] == sim_patient._EMPTY_REPLY_FALLBACK["patient"]


def test_system_prompts_declare_speech_only():
    # Contract-presence: guard against an accidental revert to the narrator prompt.
    assert "SPEECH ONLY" in sim_patient._PATIENT_SYSTEM_PROMPT
    assert "no stage directions" in sim_patient._PATIENT_SYSTEM_PROMPT.lower()
    assert "speech only" in sim_patient._NURSE_SYSTEM_PROMPT.lower()
    # The patient is now first-person, not the third-person PQM narrator.
    assert "Patient Quest Master" not in sim_patient._PATIENT_SYSTEM_PROMPT
    assert "narrate how they say it" not in sim_patient._PATIENT_SYSTEM_PROMPT
