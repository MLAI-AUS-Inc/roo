"""Opt-in scheduled worker: python -m roo.payment_reminder_worker --once."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import signal
import threading

import httpx

from .payment_reminders import DeliveryNotSent, DeliveryRejected, ReceiptStore, ReminderConfig, run_reminders


ISSUES_QUERY = """
query PaymentReminderIssues($assignee: ID!, $after: String) {
  issues(first: 100, after: $after, includeArchived: false,
    filter: {assignee: {id: {eq: $assignee}},
             state: {type: {nin: ["completed", "canceled"]}}}) {
    nodes { id identifier title url archivedAt assignee { id } state { name type } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ReminderAPI:
    """Read-only Linear queries and private Slack delivery with explicit errors.

    No transparent POST retries: a timeout after Slack accepted a message must
    leave the durable sending receipt for review, rather than send it twice.
    """

    def __init__(self, client, *, linear_key, slack_token):
        if not linear_key or not slack_token:
            raise ValueError("Payment reminder Linear and Slack credentials are required")
        self.client = client
        self.linear_key = linear_key
        self.slack_token = slack_token

    def linear(self, query, variables=None):
        response = self.client.post(
            "https://api.linear.app/graphql",
            headers={"Authorization": self.linear_key},
            json={"query": query, "variables": variables or {}},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors") or not isinstance(body.get("data"), dict):
            raise ValueError("Linear query failed or returned incomplete data")
        return body["data"]

    def slack(self, method, payload=None):
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        if method == "users.info":
            # Slack's user lookup reads query parameters, not a JSON POST body.
            response = self.client.get(
                f"https://slack.com/api/{method}", headers=headers, params=payload or {},
            )
        else:
            response = self.client.post(
                f"https://slack.com/api/{method}", headers=headers, json=payload or {},
            )
        if response.status_code == 429:
            raise DeliveryRejected("ratelimited", max(1, int(response.headers.get("Retry-After", "60"))))
        response.raise_for_status()
        body = response.json()
        if body.get("ok") is not True:
            code = body.get("error", "unknown_error")
            raise DeliveryRejected(code, 60 if code == "ratelimited" else None)
        return body

    def verify_workspace(self, config):
        if self.slack("auth.test").get("team_id") != config.slack_team_id:
            raise ValueError("Slack workspace mismatch")
        data = self.linear("query PaymentReminderOrganization { organization { id } }")
        if data["organization"]["id"] != config.linear_organization_id:
            raise ValueError("Linear organization mismatch")

    def verify_builder(self, builder, slack_team_id):
        user = self.slack("users.info", {"user": builder.slack_user_id})["user"]
        if (user.get("id") != builder.slack_user_id or user.get("team_id") != slack_team_id
                or user.get("deleted") or user.get("is_bot") or user.get("is_app_user")):
            raise ValueError("Builder Slack account is not an active workspace member")
        data = self.linear(
            "query PaymentReminderUser($id: String!) { user(id: $id) { id active } }",
            {"id": builder.linear_user_id},
        )
        if not data.get("user") or data["user"].get("id") != builder.linear_user_id or data["user"].get("active") is not True:
            raise ValueError("Builder Linear account is not active")

    def open_issues(self, assignee):
        issues = {}
        after = None
        cursors = set()
        while True:
            data = self.linear(ISSUES_QUERY, {"assignee": assignee, "after": after})["issues"]
            if not isinstance(data.get("nodes"), list):
                raise ValueError("Incomplete Linear issue page")
            for issue in data["nodes"]:
                # Defend against unexpected/mismatched API results as well as
                # filtering on the server. Status category, not its label, wins.
                if (issue.get("assignee") or {}).get("id") != assignee:
                    raise ValueError("Linear returned another builder's ticket")
                state_type = issue["state"]["type"]
                if not state_type:
                    raise ValueError("Linear returned a ticket without a status category")
                if state_type in {"completed", "canceled"} or issue.get("archivedAt"):
                    continue
                for field in ("id", "identifier", "title", "url"):
                    if not isinstance(issue.get(field), str) or not issue[field]:
                        raise ValueError("Incomplete Linear issue")
                if not isinstance(issue["state"].get("name"), str):
                    raise ValueError("Incomplete Linear status")
                issues[issue["id"]] = issue
            page = data["pageInfo"]
            if page.get("hasNextPage") is False:
                return list(issues.values())
            after = page.get("endCursor")
            if page.get("hasNextPage") is not True or not after or after in cursors:
                raise ValueError("Incomplete or repeated Linear pagination cursor")
            cursors.add(after)

    def open_dm(self, user_id):
        channel = self.slack("conversations.open", {"users": user_id})["channel"]["id"]
        if not isinstance(channel, str) or not channel.startswith("D"):
            raise ValueError("Slack did not return a private DM")
        return channel

    def post_message(self, channel, text):
        try:
            result = self.slack("chat.postMessage", {
                "channel": channel, "text": text, "parse": "none",
                "unfurl_links": False, "unfurl_media": False,
            })
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # No request reached Slack. Persist a delayed retry instead of
            # stranding the part in sending. Write/read/protocol errors remain
            # uncertain: the request could already have committed at Slack.
            raise DeliveryNotSent() from exc
        if result.get("channel") != channel or not result.get("ts"):
            raise ValueError("Slack did not confirm the message")
        return result["ts"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check the current delivery window once")
    parser.add_argument("--dry-run", action="store_true", help="Read and print previews once; never open DMs or write receipts")
    args = parser.parse_args(argv)
    config = ReminderConfig.from_env(os.environ)
    if not config.enabled:
        print(json.dumps({"status": "disabled"}))
        return 0
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    store = ReceiptStore(config.receipts_dir)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        api = ReminderAPI(client, linear_key=os.environ.get("PAYMENT_LINEAR_READ_API_KEY"),
                          slack_token=os.environ.get("PAYMENT_SLACK_BOT_TOKEN"))
        while not stop.is_set():
            try:
                result = run_reminders(config, api, store, datetime.now(timezone.utc),
                                       dry_run=args.dry_run, clock=lambda: datetime.now(timezone.utc))
            except DeliveryRejected as exc:
                result = {"status": "error", "error_type": type(exc).__name__,
                          "retry_after": exc.retry_after or 60}
            except Exception as exc:
                result = {"status": "error", "error_type": type(exc).__name__}
            # Task titles are printed only during an explicitly requested preview.
            if args.once or args.dry_run or result["status"] != "not_due":
                print(json.dumps(result), flush=True)
            if args.once or args.dry_run:
                failed = result["status"] == "error" or any(
                    item["status"] in {"error", "needs_review", "pending"}
                    for item in result.get("builders", [])
                )
                return 1 if failed else 0
            retry_after = max([60, result.get("retry_after", 0)] + [
                item.get("retry_after", 0) for item in result.get("builders", [])
            ])
            stop.wait(retry_after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
