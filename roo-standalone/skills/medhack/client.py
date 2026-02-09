"""
MedHack Client - Game state management for Guess the Diagnosis.

Uses mlai-backend as the primary state store, with local JSON fallback.
Case definitions (YAML) are always loaded locally — the backend only
tracks game state (active case, guesses, winners, etc.).
"""
import asyncio
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
    """Manages the Guess the Diagnosis game state.

    Tries mlai-backend first for all state operations.
    Falls back to local JSON if the backend is unreachable.
    """

    # Class-level lock for auto-sync operations (shared across instances)
    _sync_lock = asyncio.Lock()

    def __init__(self):
        self._cases: Optional[List[dict]] = None
        self._event_info: Optional[dict] = None
        self._backend = None
        self._backend_init = False
        # Cache the backend case data for the lifetime of this client instance
        # (one per Slack event), avoiding repeated API calls.
        self._cached_backend_case = None
        self._backend_case_fetched = False

    # ------------------------------------------------------------------
    # Backend helpers
    # ------------------------------------------------------------------

    async def _retry_with_backoff(self, func, *args, max_retries=3, **kwargs):
        """Retry a backend operation with exponential backoff.

        Args:
            func: Async function to call
            max_retries: Maximum number of retry attempts (default 3)
            *args, **kwargs: Arguments to pass to func

        Returns:
            Result from func, or None if all retries failed
        """
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries - 1:
                    # Last attempt failed, log and return None
                    print(f"⚠️ Backend operation failed after {max_retries} attempts: {e}")
                    return None
                # Exponential backoff: 0.5s, 1s, 2s
                wait_time = 0.5 * (2 ** attempt)
                print(f"⚠️ Backend operation failed (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        return None

    def _get_backend(self):
        """Lazily initialise the MLAIBackendClient."""
        if not self._backend_init:
            self._backend_init = True
            try:
                from roo.config import get_settings
                from roo.clients.mlai_backend import MLAIBackendClient
                settings = get_settings()
                if settings.MLAI_BACKEND_URL and settings.MLAI_API_KEY:
                    self._backend = MLAIBackendClient(
                        base_url=settings.MLAI_BACKEND_URL,
                        api_key=settings.MLAI_API_KEY,
                        internal_api_key=getattr(settings, 'INTERNAL_API_KEY', None) or settings.MLAI_API_KEY
                    )
            except Exception as e:
                print(f"⚠️ MedHack backend client init failed: {e}")
        return self._backend

    async def _get_backend_case(self) -> Optional[dict]:
        """Fetch the current active case from the backend (cached per request)."""
        if not self._backend_case_fetched:
            self._backend_case_fetched = True
            backend = self._get_backend()
            if backend:
                try:
                    self._cached_backend_case = await backend.medhack_get_current_case()
                except Exception as e:
                    print(f"⚠️ Backend get_current_case failed: {e}")
        return self._cached_backend_case

    def _invalidate_cache(self):
        """Reset cached backend data (call after state-changing operations)."""
        self._cached_backend_case = None
        self._backend_case_fetched = False

    # ------------------------------------------------------------------
    # Local helpers (YAML + JSON)
    # ------------------------------------------------------------------

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

    def _case_result(self, case: dict) -> dict:
        """Build the presentation dict returned by start_*_case methods."""
        result = {
            "id": case["id"],
            "difficulty": case.get("difficulty", "medium"),
            "presenting_complaint": case["presenting_complaint"],
        }
        if case.get("title"):
            result["title"] = case["title"]
        if case.get("ed_first_look"):
            result["ed_first_look"] = case["ed_first_look"]
        if case.get("image_url"):
            result["image_url"] = case["image_url"]
        return result

    def _fuzzy_match(self, guess: str, case: dict) -> bool:
        """Check if a guess matches the case diagnosis via fuzzy matching."""
        guess_clean = guess.strip().lower()
        acceptable = case.get("acceptable_answers", [])
        diagnosis = case.get("diagnosis", "").lower()

        # Exact match against acceptable answers
        if guess_clean in [a.lower() for a in acceptable]:
            return True

        # Fuzzy match against the primary diagnosis
        if SequenceMatcher(None, guess_clean, diagnosis).ratio() >= 0.75:
            return True

        # Fuzzy match against acceptable answers
        for answer in acceptable:
            if SequenceMatcher(None, guess_clean, answer.lower()).ratio() >= 0.75:
                return True

        return False

    # ------------------------------------------------------------------
    # Public async API (backend-first, local fallback)
    # ------------------------------------------------------------------

    async def get_current_case(self, today: date) -> Optional[dict]:
        """Get the active case (without the diagnosis answer)."""
        backend_case = await self._get_backend_case()
        if backend_case:
            case_id = backend_case.get("case_id")
            cases = self._load_cases()
            case = next((c for c in cases if c["id"] == case_id), None)
            if case:
                safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
                safe_case["solved"] = backend_case.get("solved", False)
                safe_case["winners"] = backend_case.get("winners", [])
                safe_case["hint_level"] = backend_case.get("hint_level", 0)
                return safe_case
            return None

        # Local fallback
        state = self._load_state()
        if state["current_case_id"] is None:
            return None
        if state["posted_date"] != today.isoformat():
            return None
        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return None

        # Sync to backend: local case exists but backend doesn't know about it
        # Use lock to prevent race condition with concurrent requests
        backend = self._get_backend()
        if backend:
            async with MedHackClient._sync_lock:
                # Re-check backend state after acquiring lock
                # (another request may have synced while we waited)
                existing_case = await self._retry_with_backoff(
                    backend.medhack_get_current_case
                )
                if existing_case is None:
                    # Try to sync with retry
                    sync_result = await self._retry_with_backoff(
                        backend.medhack_start_case,
                        state["current_case_id"],
                        "system"
                    )
                    if sync_result:
                        self._invalidate_cache()
                        print(f"🔄 Synced case #{state['current_case_id']} to backend")

        safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
        safe_case["solved"] = state["solved"]
        safe_case["winners"] = state["winners"]
        safe_case["hint_level"] = state["hint_level"]
        return safe_case

    async def get_case_for_llm(self, today: date) -> Optional[dict]:
        """Get full case data for LLM roleplay (excludes the diagnosis)."""
        backend_case = await self._get_backend_case()
        if backend_case:
            case_id = backend_case.get("case_id")
            cases = self._load_cases()
            case = next((c for c in cases if c["id"] == case_id), None)
            if case:
                safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
                safe_case["solved"] = backend_case.get("solved", False)
                safe_case["hint_level"] = backend_case.get("hint_level", 0)
                hints = case.get("hints", [])
                safe_case["revealed_hints"] = hints[:backend_case.get("hint_level", 0)]
                return safe_case
            return None

        # Local fallback
        state = self._load_state()
        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return None
        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == state["current_case_id"]), None)
        if case is None:
            return None
        safe_case = {k: v for k, v in case.items() if k not in ("diagnosis", "acceptable_answers")}
        safe_case["solved"] = state["solved"]
        safe_case["hint_level"] = state["hint_level"]
        hints = case.get("hints", [])
        safe_case["revealed_hints"] = hints[:state["hint_level"]]
        return safe_case

    async def is_user_locked_out(self, user_id: str, today: date) -> bool:
        """Check if a user has exhausted all guesses for the active case."""
        backend = self._get_backend()
        if backend:
            try:
                status = await backend.medhack_get_user_status(user_id)
                if status is not None:
                    return status.get("locked_out", False)
                # Backend returned None (e.g. 404) — fall through to local
            except Exception as e:
                print(f"⚠️ Backend is_user_locked_out failed: {e}")

        # Local fallback
        state = self._load_state()
        if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
            return False
        used = state.get("guess_counts", {}).get(user_id, 0)
        return used >= MAX_GUESSES_PER_USER

    async def set_pending_guess(self, user_id: str, guess: str) -> None:
        """Store a pending guess that needs user confirmation."""
        backend = self._get_backend()
        backend_case = await self._get_backend_case()
        if backend and backend_case:
            try:
                await backend.medhack_set_pending_guess(
                    backend_case["case_id"], user_id, guess
                )
                return
            except Exception as e:
                print(f"⚠️ Backend set_pending_guess failed: {e}")

        # Local fallback
        state = self._load_state()
        pending = state.get("pending_guesses", {})
        pending[user_id] = guess
        state["pending_guesses"] = pending
        self._save_state(state)

    async def get_pending_guess(self, user_id: str) -> Optional[str]:
        """Get the pending guess for a user, if any."""
        backend = self._get_backend()
        if backend:
            try:
                status = await backend.medhack_get_user_status(user_id)
                if status is not None:
                    return status.get("pending_guess")
                # Backend returned None (e.g. 404) — fall through to local
            except Exception as e:
                print(f"⚠️ Backend get_pending_guess failed: {e}")

        # Local fallback
        state = self._load_state()
        return state.get("pending_guesses", {}).get(user_id)

    async def clear_pending_guess(self, user_id: str) -> None:
        """Clear a user's pending guess."""
        backend = self._get_backend()
        backend_case = await self._get_backend_case()
        if backend and backend_case:
            try:
                await backend.medhack_clear_pending_guess(
                    backend_case["case_id"], user_id
                )
                return
            except Exception as e:
                print(f"⚠️ Backend clear_pending_guess failed: {e}")

        # Local fallback
        state = self._load_state()
        pending = state.get("pending_guesses", {})
        pending.pop(user_id, None)
        state["pending_guesses"] = pending
        self._save_state(state)

    async def check_guess(self, user_id: str, guess: str, today: date) -> Dict[str, Any]:
        """Check a guess against the current diagnosis. Records result on backend."""
        backend = self._get_backend()
        backend_case = await self._get_backend_case()

        # Determine the active case_id
        if backend_case:
            case_id = backend_case["case_id"]
        else:
            state = self._load_state()
            if state["current_case_id"] is None or state["posted_date"] != today.isoformat():
                return {"correct": False, "already_solved": False, "guesses_remaining": 0, "message": "No active case today."}
            case_id = state["current_case_id"]

        cases = self._load_cases()
        case = next((c for c in cases if c["id"] == case_id), None)
        if case is None:
            return {"correct": False, "already_solved": False, "guesses_remaining": 0, "message": "Case not found."}

        # Fuzzy match locally (backend doesn't know diagnoses)
        is_correct = self._fuzzy_match(guess, case)

        # Try to record on backend with retry logic
        if backend and backend_case:
            # Invalidate cache BEFORE making state changes to prevent stale reads
            self._invalidate_cache()

            submit_result = await self._retry_with_backoff(
                backend.medhack_submit_guess,
                case_id, user_id, guess, is_correct
            )
            if submit_result:
                if is_correct:
                    is_first = not backend_case.get("solved", False)
                    # Record winner with retry
                    await self._retry_with_backoff(
                        backend.medhack_record_winner,
                        case_id, user_id, is_first
                    )
                    return {
                        "correct": True,
                        "already_solved": False,
                        "is_first_solver": is_first,
                        "diagnosis": case["diagnosis"],
                        "message": "correct",
                    }
                else:
                    return {
                        "correct": False,
                        "already_solved": False,
                        "guesses_remaining": 0,
                        "message": "incorrect",
                    }

        # Local fallback
        if not backend_case:
            state = self._load_state()
        else:
            state = self._load_state()

        if state.get("solved") and user_id in state.get("winners", []):
            return {"correct": False, "already_solved": True, "guesses_remaining": 0, "message": "You've already solved today's case!"}

        guess_counts = state.get("guess_counts", {})
        used = guess_counts.get(user_id, 0)
        if used >= MAX_GUESSES_PER_USER:
            return {"correct": False, "already_solved": False, "guesses_remaining": 0, "message": "no_guesses_remaining"}

        if is_correct:
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

        # Wrong guess — increment counter locally
        guess_counts[user_id] = used + 1
        state["guess_counts"] = guess_counts
        self._save_state(state)
        remaining = MAX_GUESSES_PER_USER - (used + 1)
        return {"correct": False, "already_solved": False, "guesses_remaining": remaining, "message": "incorrect"}

    async def advance_hint(self, today: date) -> Optional[str]:
        """Advance to the next hint level and return the new hint (if any)."""
        # For now, hints are local-only (backend tracks hint_level but
        # the advance endpoint isn't built yet).
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

    async def start_new_case(self, today: date, admin_slack_id: Optional[str] = None) -> Optional[dict]:
        """Select and activate the next unplayed case."""
        state = self._load_state()
        cases = self._load_cases()

        played = set(state.get("played_case_ids", []))
        available = [c for c in cases if c["id"] not in played]

        if not available:
            played = set()
            available = cases

        if not available:
            return None

        new_case = available[0]

        # Try backend first with retry logic
        backend = self._get_backend()
        if backend:
            # Invalidate cache before making state changes
            self._invalidate_cache()
            sender = admin_slack_id or "system"
            await self._retry_with_backoff(
                backend.medhack_start_case,
                new_case["id"],
                sender
            )

        # Always update local state too (fallback + played_case_ids tracking)
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

        return self._case_result(new_case)

    async def start_specific_case(self, case_id: int, today: date, admin_slack_id: Optional[str] = None) -> Optional[dict]:
        """Start a specific case by its ID (for admin manual advancement)."""
        cases = self._load_cases()
        new_case = next((c for c in cases if c["id"] == case_id), None)
        if new_case is None:
            return None

        # Try backend first with retry logic
        backend = self._get_backend()
        if backend:
            # Invalidate cache before making state changes
            self._invalidate_cache()
            sender = admin_slack_id or "system"
            await self._retry_with_backoff(
                backend.medhack_start_case,
                new_case["id"],
                sender
            )

        # Always update local state too
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

        return self._case_result(new_case)

    def get_all_case_ids(self) -> List[int]:
        """Return all available case IDs from cases.yaml."""
        return [c["id"] for c in self._load_cases()]
