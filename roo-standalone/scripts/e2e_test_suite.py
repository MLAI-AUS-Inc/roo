#!/usr/bin/env python3
from pathlib import Path
import asyncio
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from roo import quests


class FakeSettings:
    MLAI_BACKEND_URL = "http://example"
    MLAI_API_KEY = "test-key"
    INTERNAL_API_KEY = "internal-key"


class FakeBackendStore:
    def __init__(self):
        self.reset()

    def reset(self):
        self.admin_allowances = {
            "ADMIN_UNLIMITED": None,
            "ADMIN_LIMITED": 3,
        }
        self.remaining = {"ADMIN_LIMITED": 3}
        self.awards = []

    def is_admin(self, slack_id: str) -> bool:
        return slack_id in self.admin_allowances

    def get_allowance(self, slack_id: str) -> dict:
        if slack_id not in self.admin_allowances:
            return {"error": "Not a points admin"}
        allowance = self.admin_allowances[slack_id]
        remaining = None if allowance is None else self.remaining.get(slack_id, 0)
        return {"allowance": allowance, "remaining": remaining}

    def consume(self, slack_id: str, points: int) -> None:
        allowance = self.admin_allowances.get(slack_id)
        if allowance is None:
            return
        self.remaining[slack_id] = max(0, self.remaining.get(slack_id, 0) - points)


STORE = FakeBackendStore()


class FakeBackendClient:
    def __init__(self, **_kwargs):
        pass

    async def is_admin(self, slack_user_id: str) -> bool:
        return STORE.is_admin(slack_user_id)

    async def get_admin_allowance(self, slack_user_id: str) -> dict:
        return STORE.get_allowance(slack_user_id)

    async def award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str
    ) -> dict:
        if not await self.is_admin(admin_slack_id):
            raise PermissionError("Not a points admin")
        if admin_slack_id == target_slack_id and points > 0:
            raise ValueError("Cannot self-award points")
        if points < 0:
            raise ValueError("Negative awards disabled")

        allowance = await self.get_admin_allowance(admin_slack_id)
        if "error" in allowance:
            raise PermissionError(allowance["error"])

        remaining = allowance.get("remaining")
        if remaining is not None:
            if remaining <= 0:
                raise ValueError("Weekly allowance exhausted")
            if points > remaining:
                raise ValueError("Award exceeds remaining allowance")
            STORE.consume(admin_slack_id, points)

        STORE.awards.append({
            "admin_slack_id": admin_slack_id,
            "target_slack_id": target_slack_id,
            "points": points,
            "reason": reason,
        })
        return {"ok": True}

    async def system_award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str
    ) -> dict:
        STORE.awards.append({
            "admin_slack_id": admin_slack_id,
            "target_slack_id": target_slack_id,
            "points": points,
            "reason": reason,
        })
        return {"ok": True}


def reset_quest_state():
    quests._link_love_threads.clear()
    quests._link_love_daily_posts.clear()
    quests._link_love_thread_rewards.clear()
    quests._quest_progress.clear()
    quests._completed_quests.clear()


def patch_quests(messages):
    async def noop_update_progress(*_args, **_kwargs):
        return None

    quests.get_settings = lambda: FakeSettings()
    quests.MLAIBackendClient = FakeBackendClient
    quests.get_bot_user_id = lambda: "BOT"
    quests.get_channel_id = (
        lambda name: "C123" if name == quests.LINK_LOVE_CHANNEL_NAME else None
    )
    quests.post_message = lambda **kwargs: messages.append(kwargs)
    quests.get_thread_messages = lambda *_a, **_k: []
    quests._update_progress = noop_update_progress


def expect(condition: bool, label: str, failures: list):
    if not condition:
        failures.append(label)


async def run_admin_scenarios(failures):
    STORE.reset()
    client = FakeBackendClient()

    try:
        await client.award_points(
            admin_slack_id="ADMIN_UNLIMITED",
            target_slack_id="USER_A",
            points=50,
            reason="Unlimited admin award"
        )
        expect(True, "unlimited admin award succeeded", failures)
    except Exception as exc:
        failures.append(f"unlimited admin award failed: {exc}")

    try:
        await client.award_points(
            admin_slack_id="ADMIN_LIMITED",
            target_slack_id="USER_B",
            points=2,
            reason="Limited admin award"
        )
        expect(True, "limited admin award within allowance", failures)
    except Exception as exc:
        failures.append(f"limited admin award failed: {exc}")

    try:
        await client.award_points(
            admin_slack_id="ADMIN_LIMITED",
            target_slack_id="USER_B",
            points=2,
            reason="Limited admin overage"
        )
        failures.append("limited admin overage should have failed")
    except ValueError:
        expect(True, "limited admin overage blocked", failures)
    except Exception as exc:
        failures.append(f"limited admin overage wrong error: {exc}")

    try:
        await client.award_points(
            admin_slack_id="NON_ADMIN",
            target_slack_id="USER_C",
            points=1,
            reason="Non-admin award"
        )
        failures.append("non-admin award should have failed")
    except PermissionError:
        expect(True, "non-admin award blocked", failures)
    except Exception as exc:
        failures.append(f"non-admin award wrong error: {exc}")


async def run_link_love_scenarios(failures):
    STORE.reset()
    reset_quest_state()
    messages = []
    patch_quests(messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700001000.0001",
        "text": "Founder share",
    })
    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700001001.0002",
        "thread_ts": "1700001000.0001",
        "text": "Liked and shared!",
    })
    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700001002.0003",
        "thread_ts": "1700001000.0001",
        "text": "Shared again.",
    })
    await quests.handle_quests({
        "type": "message",
        "user": "U3",
        "channel": "C123",
        "ts": "1700001003.0004",
        "thread_ts": "1700001000.0001",
        "text": "Shared too.",
    })
    await quests.handle_quests({
        "type": "message",
        "user": "U4",
        "channel": "C123",
        "ts": "1700001004.0005",
        "thread_ts": "1700001000.0001",
        "text": "Nice post!",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700001100.0001",
        "text": "Another share",
    })
    await quests.handle_quests({
        "type": "message",
        "user": "U5",
        "channel": "C123",
        "ts": "1700001101.0002",
        "thread_ts": "1700001100.0001",
        "text": "Shared this.",
    })

    awarded_targets = [award["target_slack_id"] for award in STORE.awards]
    expect(len(STORE.awards) == 2, "link love awards count == 2", failures)
    expect(set(awarded_targets) == {"U2", "U3"}, "link love awards to U2/U3", failures)
    expect(len(messages) == 2, "link love thread replies count == 2", failures)


async def main():
    failures = []
    await run_admin_scenarios(failures)
    await run_link_love_scenarios(failures)

    if failures:
        print("E2E TEST SUITE RESULT: FAIL")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)

    print("E2E TEST SUITE RESULT: PASS")


if __name__ == "__main__":
    asyncio.run(main())
