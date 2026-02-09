"""
MedHack Client - Game state management for Guess the Diagnosis.

Handles case loading, game state persistence, guess checking, and event info.
"""
import json
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any, List

import yaml
from difflib import SequenceMatcher


SKILL_DIR = Path(__file__).parent
CASES_FILE = SKILL_DIR / "cases.yaml"
EVENT_INFO_FILE = SKILL_DIR / "event_info.yaml"

# Game state goes in /app/data (writable volume) in production, falls back to skill dir for local dev
DATA_DIR = Path("/app/data") if Path("/app/data").exists() else SKILL_DIR
GAME_STATE_FILE = DATA_DIR / "medhack_game_state.json"

# Points awarded for a correct diagnosis
DIAGNOSIS_WIN_POINTS = 12

# Maximum guesses per user per case
MAX_GUESSES_PER_USER = 1


class MedHackClient:
    """Manages the Guess the Diagnosis game state."""

    def __init__(self):
        self._cases: Optional[List[dict]] = None
        self._event_info: Optional[dict] = None

    def _load_cases(self) -> List[dict]:
        if self._cases is None:
            with open(CASES_FILE, "r") as f:
                data = yaml.safe_load(f)
            self._cases = data.get("cases", [])
        return self._cases

    def _load_state(self) -> dict:
        if GAME_STATE_FILE.exists():
            with open(GAME_STATE_FILE, "r") as f:
                return json.load(f)
        return {
            "current_case_id": None,
            "posted_date": None,
            "winners": [],
            "solved": False,
            "hint_level": 0,
            "played_case_ids": [],
            "guess_counts": {},
            "pending_guesses": {},
        }

    def _save_state(self, state: dict) -> None:
        with open(GAME_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def load_event_info(self) -> dict:
        if self._event_info is None:
            with open(EVENT_INFO_FILE, "r") as f:
                self._event_info = yaml.safe_load(f)
        return self._event_info

    def get_current_case(self, today: date) -> Optional[dict]:
        """Get today's active case (without the diagnosis answer).

        Returns None if no case is active for today.
        """
        state = self._load_state()

        if state["current_case_id"] is None:
            return None

        if state["posted_date"] != today.isoformat():
            return None

        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return None

        # Return case data without the answer
        safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
        safe_case["solved"] = state["solved"]
        safe_case["winners"] = state["winners"]
        safe_case["hint_level"] = state["hint_level"]
        return safe_case

    def get_case_for_llm(self, today: date) -> Optional[dict]:
        """Get full case data for LLM roleplay (excludes the diagnosis).

        The LLM uses this to answer questions about the patient in character.
        The diagnosis is checked separately via check_guess().
        """
        state = self._load_state()
        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return None

        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return None

        # Include everything except the diagnosis for LLM context
        safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
        safe_case["solved"] = state["solved"]
        safe_case["hint_level"] = state["hint_level"]

        # Include progressive hints up to current level
        hints = case.get("hints", [])
        safe_case["revealed_hints"] = hints[:state["hint_level"]]
        return safe_case

    def get_user_guesses_remaining(self, user_id: str, today: date) -> int:
        """Return how many guesses a user has left for today's case."""
        state = self._load_state()
        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return 0
        guess_counts = state.get("guess_counts", {})
        used = guess_counts.get(user_id, 0)
        return max(0, MAX_GUESSES_PER_USER - used)

    def is_user_locked_out(self, user_id: str, today: date) -> bool:
        """Check if a user has exhausted all guesses for today's case."""
        return self.get_user_guesses_remaining(user_id, today) <= 0

    def set_pending_guess(self, user_id: str, guess: str) -> None:
        """Store a pending guess that needs user confirmation before locking in."""
        state = self._load_state()
        pending = state.get("pending_guesses", {})
        pending[user_id] = guess
        state["pending_guesses"] = pending
        self._save_state(state)

    def get_pending_guess(self, user_id: str) -> Optional[str]:
        """Get the pending guess for a user, if any."""
        state = self._load_state()
        return state.get("pending_guesses", {}).get(user_id)

    def clear_pending_guess(self, user_id: str) -> None:
        """Clear a user's pending guess."""
        state = self._load_state()
        pending = state.get("pending_guesses", {})
        pending.pop(user_id, None)
        state["pending_guesses"] = pending
        self._save_state(state)

    def check_guess(self, user_id: str, guess: str, today: date) -> Dict[str, Any]:
        """Check if a guess matches the current case's diagnosis.

        Returns:
            dict with keys: correct (bool), already_solved (bool),
            guesses_remaining (int), message (str)
        """
        state = self._load_state()

        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return {"correct": False, "already_solved": False, "guesses_remaining": 0, "message": "No active case today."}

        if state["solved"] and user_id in state["winners"]:
            return {"correct": False, "already_solved": True, "guesses_remaining": 0, "message": "You've already solved today's case!"}

        # Check guess limit
        guess_counts = state.get("guess_counts", {})
        used = guess_counts.get(user_id, 0)
        if used >= MAX_GUESSES_PER_USER:
            return {
                "correct": False,
                "already_solved": False,
                "guesses_remaining": 0,
                "message": "no_guesses_remaining",
            }

        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return {"correct": False, "already_solved": False, "guesses_remaining": 0, "message": "Case not found."}

        # Check against acceptable answers using fuzzy matching
        guess_clean = guess.strip().lower()
        acceptable = case.get("acceptable_answers", [])
        diagnosis = case.get("diagnosis", "").lower()

        is_correct = False

        # Exact match against acceptable answers
        if guess_clean in [a.lower() for a in acceptable]:
            is_correct = True

        # Fuzzy match against the primary diagnosis
        if not is_correct:
            ratio = SequenceMatcher(None, guess_clean, diagnosis).ratio()
            if ratio >= 0.75:
                is_correct = True

        # Fuzzy match against acceptable answers
        if not is_correct:
            for answer in acceptable:
                ratio = SequenceMatcher(None, guess_clean, answer.lower()).ratio()
                if ratio >= 0.75:
                    is_correct = True
                    break

        if is_correct:
            return self._record_win(state, user_id, case)

        # Wrong guess - increment counter
        guess_counts[user_id] = used + 1
        state["guess_counts"] = guess_counts
        self._save_state(state)

        remaining = MAX_GUESSES_PER_USER - (used + 1)
        return {"correct": False, "already_solved": False, "guesses_remaining": remaining, "message": "incorrect"}

    def _record_win(self, state: dict, user_id: str, case: dict) -> Dict[str, Any]:
        """Record a winner and save state."""
        is_first = not state["solved"]
        state["solved"] = True
        if user_id not in state["winners"]:
            state["winners"].append(user_id)
        self._save_state(state)

        return {
            "correct": True,
            "already_solved": False,
            "is_first_solver": is_first,
            "diagnosis": case["diagnosis"],
            "message": "correct",
        }

    def advance_hint(self, today: date) -> Optional[str]:
        """Advance to the next hint level and return the new hint (if any)."""
        state = self._load_state()
        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return None

        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return None

        hints = case.get("hints", [])
        if state["hint_level"] < len(hints):
            state["hint_level"] += 1
            self._save_state(state)
            return hints[state["hint_level"] - 1]
        return None

    def start_new_case(self, today: date) -> Optional[dict]:
        """Select and activate the next unplayed case.

        Returns the new case's presenting complaint for posting, or None
        if all cases have been played.
        """
        state = self._load_state()
        cases = self._load_cases()

        played = set(state.get("played_case_ids", []))
        available = [c for c in cases if c["id"] not in played]

        if not available:
            # All cases played - reset
            played = set()
            available = cases

        if not available:
            return None

        new_case = available[0]

        state["current_case_id"] = new_case["id"]
        state["posted_date"] = today.isoformat()
        state["winners"] = []
        state["solved"] = False
        state["hint_level"] = 0
        state["guess_counts"] = {}
        state["pending_guesses"] = {}
        state.setdefault("played_case_ids", [])
        if new_case["id"] not in state["played_case_ids"]:
            state["played_case_ids"].append(new_case["id"])
        self._save_state(state)

        result = {
            "id": new_case["id"],
            "difficulty": new_case.get("difficulty", "medium"),
            "presenting_complaint": new_case["presenting_complaint"],
        }
        if new_case.get("title"):
            result["title"] = new_case["title"]
        if new_case.get("ed_first_look"):
            result["ed_first_look"] = new_case["ed_first_look"]
        if new_case.get("image_url"):
            result["image_url"] = new_case["image_url"]
        return result

    def start_specific_case(self, case_id: int, today: date) -> Optional[dict]:
        """Start a specific case by its ID (for admin manual advancement).

        Returns the case presentation data, or None if case_id not found.
        """
        cases = self._load_cases()
        new_case = next((c for c in cases if c["id"] == case_id), None)
        if new_case is None:
            return None

        state = self._load_state()
        state["current_case_id"] = new_case["id"]
        state["posted_date"] = today.isoformat()
        state["winners"] = []
        state["solved"] = False
        state["hint_level"] = 0
        state["guess_counts"] = {}
        state["pending_guesses"] = {}
        state.setdefault("played_case_ids", [])
        if new_case["id"] not in state["played_case_ids"]:
            state["played_case_ids"].append(new_case["id"])
        self._save_state(state)

        result = {
            "id": new_case["id"],
            "difficulty": new_case.get("difficulty", "medium"),
            "presenting_complaint": new_case["presenting_complaint"],
        }
        if new_case.get("title"):
            result["title"] = new_case["title"]
        if new_case.get("ed_first_look"):
            result["ed_first_look"] = new_case["ed_first_look"]
        if new_case.get("image_url"):
            result["image_url"] = new_case["image_url"]
        return result

    def get_all_case_ids(self) -> List[int]:
        """Return all available case IDs from cases.yaml."""
        return [c["id"] for c in self._load_cases()]
