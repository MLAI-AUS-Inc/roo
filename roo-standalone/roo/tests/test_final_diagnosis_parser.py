"""parse_final_diagnosis: the deterministic gate between chat and the one-shot
contest. Table-driven over real phrasings from the 2026-07-14 prod transcript
where "for sash is addisonian crisis" was recorded verbatim and burnt a
correct answer. No LLM anywhere in these paths.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.sim_patient import check_guess
from roo.ward_agents import parse_final_diagnosis

SASH = {"id": 1, "patient": {"name": "Sasha 'Sash' Nguyen"}}
LEILA = {"id": 2, "patient": {"name": "Leila Farouk"}}
OPEN_CASES = [SASH, LEILA]


@pytest.mark.parametrize("question,diagnosis,case_id", [
    # The two messages that burnt real books — now clean and targeted.
    ("My final diagnosis for leila is crohns disease", "crohns disease", 2),
    ("my final diagnosis for sash is addisonian crisis", "addisonian crisis", 1),
    # Natural finality that used to deflect forever.
    ("my diagnosis is gastritis", "gastritis", None),
    ("for sash i diagnose addisonian crisis", "addisonian crisis", 1),
    # Original supported phrasings keep working (regression).
    ("My final answer is adrenal crisis.", "adrenal crisis", None),
    ("final diagnosis: methemoglobinemia", "methemoglobinemia", None),
    ("I'm going with salicylate toxicity", "salicylate toxicity", None),
    ("submit my diagnosis as lupus", "lupus", None),
    ("lock in adrenal crisis", "adrenal crisis", None),
    ("please record my final diagnosis: appendicitis", "appendicitis", None),
    # Reverse frame.
    ("addisonian crisis is my final answer", "addisonian crisis", None),
    # Target variants.
    ("final diagnosis for Leila: acute intermittent porphyria",
     "acute intermittent porphyria", 2),
    ("my final answer is sash has adrenal crisis", "adrenal crisis", 1),
    ("my final diagnosis is crohns disease for leila", "crohns disease", 2),
    # Typo in the name still resolves (fuzzy ≥ 0.8).
    ("my final diagnosis for leela is crohns disease", "crohns disease", 2),
    # The 2026-07-14 second burn: trailing declaration frames must be shaved.
    ("submit addisonian crisis as final diagnosis for sash", "addisonian crisis", 1),
    ("lock in crohns disease as my final answer", "crohns disease", None),
    # Same-message anaphora: the declaration's antecedent is one sentence back.
    ("I think sash has addisonian crisis. submit that", "addisonian crisis", 1),
])
def test_final_declarations_parse_clean(question, diagnosis, case_id):
    parsed = parse_final_diagnosis(question, OPEN_CASES)
    assert parsed is not None, question
    assert parsed["diagnosis"] == diagnosis
    assert parsed["case_id"] == case_id


@pytest.mark.parametrize("question", [
    # Theories must never arm the one-shot confirmation.
    "i guess leila has gastritis",
    "I think it's Addison's disease",
    "Is it adrenal crisis?",
    "Could it be lupus?",
    "what do leilas bloods look like?",
    "whats leilas obs?",
    "",
    "final",
])
def test_non_final_messages_do_not_parse(question):
    assert parse_final_diagnosis(question, OPEN_CASES) is None


def test_unknown_name_stays_in_the_diagnosis():
    # "for" clauses that do not name an open patient are left untouched.
    parsed = parse_final_diagnosis(
        "my final diagnosis is nausea for weeks", OPEN_CASES,
    )
    assert parsed == {"diagnosis": "nausea for weeks", "case_id": None}


def test_anaphora_resolves_from_the_latest_theory():
    history = [
        {"role": "player", "text": "whats leilas obs?"},
        {"role": "patient", "text": "Heart rate 128."},
        {"role": "player", "text": "for sash i diagnose addisonian crisis"},
        {"role": "patient", "text": "If that's your final diagnosis, say so."},
    ]
    parsed = parse_final_diagnosis(
        "thats my final diagnosis", OPEN_CASES, history,
    )
    assert parsed == {"diagnosis": "addisonian crisis", "case_id": 1}


def test_anaphora_resolves_theory_phrasings_too():
    history = [
        {"role": "player", "text": "i guess leila has gastritis"},
        {"role": "patient", "text": "I can't confirm or rule out a theory."},
    ]
    parsed = parse_final_diagnosis(
        "that is my final answer", OPEN_CASES, history,
    )
    assert parsed == {"diagnosis": "gastritis", "case_id": 2}


def test_anaphora_without_antecedent_stays_unparsed():
    assert parse_final_diagnosis("thats my final diagnosis", OPEN_CASES, []) is None
    assert parse_final_diagnosis(
        "thats my final diagnosis",
        OPEN_CASES,
        [{"role": "player", "text": "hello"}],
    ) is None


def test_check_guess_ignores_historical_target_pollution():
    case = {
        "diagnosis": "Adrenal Crisis",
        "acceptable_answers": ["adrenal crisis", "addisonian crisis"],
    }
    # The exact burnt strings from prod — both pollution shapes must match.
    assert check_guess("for sash is addisonian crisis", case) is True
    assert check_guess("addisonian crisis as final diagnosis", case) is True
    assert check_guess("addisonian crisis", case) is True
    assert check_guess("gastroenteritis", case) is False
