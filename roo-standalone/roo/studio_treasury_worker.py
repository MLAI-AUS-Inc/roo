"""Private treasurer worker. Review, authorise handoffs and verify receipts."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import os
import time
from urllib.parse import urlsplit

import requests
from pypdf import PdfReader
from dotenv import dotenv_values
from slack_sdk import WebClient

from .payment_reminders import ReceiptStore
from .timesheets import fingerprint


REASONS = {
    "coverage_unknown": "Customer opening balance needs checking",
    "customer_coverage_missing": "Customer hours or labour revenue are short",
    "customer_credit_changed": "Customer credit changed after earlier allocations",
    "approved_work_missing": "Approved Linear work is missing",
    "period_allocation_needed": "Choose the tickets included in this period total",
    "work_already_claimed_or_hours_mismatch": "Ticket hours differ or were already claimed",
    "payment_in_progress": "This invoice already has a payment handoff",
    "invoice_total_mismatch": "The invoice lines differ from its total",
}

ERRORS = {
    "review_expired": "That review expired. Ask me to check the builder invoices again.",
    "sources_changed_request_new_review": "The invoice or work changed. Ask for a fresh invoice check before paying.",
    "coverage_changed_request_new_review": "Another payment changed the available coverage. Ask for a fresh invoice check.",
    "studio_setup_or_review_missing": "The Studio setup or payment review is missing. The treasurer needs to check it.",
    "studio_request_not_authorized": "This private Studio request is not authorised for your account.",
    "bank_receipt_mismatch": "The bank receipt does not match the approved amount, payee or reference. Review that transfer before retrying.",
    "approved_time_requires_correction": "That time was already approved. The treasurer needs to record a correction.",
    "meeting_time_note_needs_review": "The Linear attendance note needs checking before this time can be approved.",
}


def format_review(result):
    if "calendar_scan" in result:
        lines = ["Calendar check completed."]
        lines.extend("• " + row["title"] + " · " + row["start"][:10] + " · " + row["action"] for row in result["calendar_scan"])
        return "\n".join(lines)
    if "linked" in result:
        return "Meeting linked to " + result["linked"]["identifier"] + "."
    if "meetings" in result:
        return "Link or confirm your meetings below." if result["meetings"] or result.get("links") else "Your meeting time is up to date."
    if "attendance" in result:
        return "Meeting time saved" + (" and approved." if result["attendance"]["approved"] else " for approval.")
    invoices = result["invoices"]
    ready = [row for row in invoices if row["status"] == "ready"]
    needs = [row for row in invoices if row["status"] == "needs_review"]
    if result["status"] in {"reserved", "partial"}:
        lines = ["Customer coverage reserved. Use these references for the bank transfers:"]
        for handoff in result.get("handoffs", []):
            lines.append(f"• {handoff['number']} · {handoff['currency']} {handoff['cents']/100:,.2f} · `{handoff['reference']}` · {handoff['status']}")
        lines.append(f"After settlement, reply `record payment {result['id']} INVOICE XERO-BANK-TRANSACTION-ID`.")
        return "\n".join(lines)
    if result["status"] == "paid":
        return "Payment confirmed against the bank receipt. The matched work and customer allowance have been used once."
    lines = [f"{len(invoices)} invoices checked. {len(ready)} ready for AUD {result['ready_cents']/100:,.2f}. {len(needs)} need review."]
    for row in invoices:
        reasons = {r["code"] for r in row.get("lines", []) if r["code"] != "matched"}
        reasons.update(r["code"] for r in row.get("errors", []))
        text = ", ".join(REASONS.get(code, code.replace("_", " ")) for code in sorted(reasons))
        uncovered = sum(entry.get("uncovered_units", 0) for entry in row.get("lines", [])) / 3600
        if uncovered:
            text += f" ({uncovered:g} hours without cover)"
        lines.append(f"• {row['number']} · {text or row['status'].replace('_', ' ')}")
        if result.get("show_details"):
            if row.get("source"):
                lines.append(row["source"])
            for entry in row.get("lines", []):
                approved = sum(w["approved_units"] for w in entry.get("work", [])) / 3600
                requested = entry["requested_units"] / 3600
                lines.append(f"  {entry.get('issue') or 'Period total'} · {requested:g} requested · {approved:g} approved · "
                             + REASONS.get(entry["code"], entry["code"].replace("_", " ")))
    if result.get("exceptions"):
        lines.append(f"{len(result['exceptions'])} additional source items need review.")
    if ready:
        lines.append("Pay ready invoices reserves coverage and prepares your bank handoff.")
    return "\n".join(lines)


def blocks(result):
    text = format_review(result)
    output = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
    if "meetings" in result:
        if result.get("scan", {}).get("mode") == "review" and any(row["action"] in {"planned", "needs_review"} for row in result.get("calendar_scan", [])):
            output.append({"type": "actions", "elements": [{"type": "button",
                "text": {"type": "plain_text", "text": "Add missing meetings"}, "action_id": "studio_meeting_add_scan",
                "value": json.dumps({"start": result["scan"]["start"], "end": result["scan"]["end"]})}]})
        for row in result.get("links", [])[:8]:
            elements = [{"type": "button", "text": {"type": "plain_text", "text": "Link " + candidate["identifier"]},
                "action_id": "studio_meeting_link", "value": json.dumps({"meeting": row["key"], "issue": candidate["id"]})}
                for candidate in row["candidates"]]
            if elements:
                output.append({"type": "section", "text": {"type": "plain_text", "text": row["title"]}})
                output.append({"type": "actions", "elements": elements})
        for meeting in result["meetings"][:12]:
            output.append({"type": "section", "text": {"type": "plain_text",
                "text": meeting["title"] + " · " + str(meeting["minutes"]) + " min"}})
            value = {"meeting": meeting["key"], "minutes": meeting["minutes"],
                     "builder": meeting["builder"], "approved": meeting["approve"]}
            output.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": ("Approve " if meeting["approve"] else "Confirm ") + str(meeting["minutes"]) + " min"},
                 "action_id": "studio_meeting_confirm", "value": json.dumps(value)},
                {"type": "button", "text": {"type": "plain_text", "text": "Edit time"},
                 "action_id": "studio_meeting_edit", "value": json.dumps(value)}]})
        return output
    if result.get("status") == "preview" and any(r["status"] == "ready" for r in result["invoices"]):
        output.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Prepare payments"},
             "action_id": "studio_treasury_authorize", "value": result["id"]},
            {"type": "button", "text": {"type": "plain_text", "text": "Review details"},
             "action_id": "studio_treasury_review", "value": result["id"]}]})
    return output


class TreasuryWorker:
    def __init__(self, env):
        self.env = env
        self.queue = ReceiptStore(Path(env["STUDIO_TREASURY_QUEUE_DIR"]))
        self.slack = WebClient(token=env["SLACK_BOT_TOKEN"], timeout=20)
        self.url = env["MLAI_BACKEND_URL"].rstrip("/") + "/api/integrations/studio-treasury/action"
        if urlsplit(self.url).scheme != "https" and urlsplit(self.url).hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("treasury_backend_requires_https")
        if len(env["STUDIO_TREASURY_KEY"]) < 32:
            raise ValueError("private_treasury_key_required")

    def sealed_artifacts(self):
        result = {}
        root = self.env.get("STUDIO_TREASURY_FINALS_DIR")
        if not root:
            return result
        files = sorted(Path(root).glob("*.json"))
        if len(files) > 1000:
            raise ValueError("invoice_artifact_limit")
        for path in files:
            artifact = json.loads(path.read_text())
            invoice = artifact.get("invoice")
            if not invoice or artifact.get("draft") or artifact["team"] != self.env["TIMESHEET_SLACK_TEAM_ID"]:
                continue
            reader = PdfReader(io.BytesIO(base64.b64decode(artifact["pdf"])))
            static = hashlib.sha256(b"".join(page.get_contents().get_data() for page in reader.pages)).hexdigest()
            result[invoice["reference"]] = {"invoice": invoice, "static_hash": static}
        return result

    def tick(self):
        for path in sorted(self.queue.directory.glob("*.json")):
            key = path.stem
            with self.queue.locked(key) as acquired:
                if not acquired:
                    continue
                job = self.queue.read(key)
                if job["status"] in {"sent", "sending", "error"}:
                    continue
                if job.get("retry_at", 0) > time.time():
                    continue
                if job["workspace"] != self.env["TIMESHEET_SLACK_TEAM_ID"]:
                    raise ValueError("workspace_mismatch")
                if self.slack.auth_test()["team_id"] != job["workspace"]:
                    raise ValueError("slack_workspace_mismatch")
                user = self.slack.users_info(user=job["actor"])["user"]
                if user.get("deleted") or user.get("is_bot") or user.get("team_id") != job["workspace"]:
                    raise ValueError("recipient_unavailable")
                channel = self.slack.conversations_open(users=[job["actor"]])["channel"]["id"]
                if "result" not in job:
                    response = requests.post(self.url, json={"workspace": job["workspace"], "actor": job["actor"],
                        "request_key": key, **job["command"], "sealed_artifacts": self.sealed_artifacts()}, timeout=(3, 120), allow_redirects=False,
                        headers={"X-Studio-Treasury-Key": self.env["STUDIO_TREASURY_KEY"]})
                    if response.status_code >= 500 or response.status_code == 429:
                        job["retry_at"] = time.time() + 60
                        self.queue.write(key, job)
                        continue
                    if response.status_code >= 400:
                        job.update(status="error", error="review_needs_retry_or_setup")
                        self.queue.write(key, job)
                        try:
                            message = ERRORS.get(response.json().get("error"))
                        except ValueError:
                            message = None
                        self.slack.chat_postMessage(channel=channel, text=message or "I couldn’t complete this Studio check. The treasurer needs to review the source or setup, then ask again.")
                        continue
                    job["result"] = response.json()
                    for reviewer in job["result"].get("notify_reviewers", []):
                        notification_key = fingerprint({"reviewer": reviewer, "request": key})
                        with self.queue.locked(notification_key) as acquired_notification:
                            if acquired_notification and self.queue.read(notification_key) is None:
                                self.queue.write(notification_key, {"workspace": job["workspace"], "actor": reviewer,
                                    "request_key": notification_key, "command": {"operation": "meeting_review"}, "status": "pending"})
                    if job["command"]["operation"] == "show":
                        job["result"]["show_details"] = True
                    self.queue.write(key, job)
                # An uncertain delivery stays inspectable and never resubmits a payment.
                job["status"] = "sending"
                self.queue.write(key, job)
                sent = self.slack.chat_postMessage(channel=channel, text=format_review(job["result"]),
                    blocks=blocks(job["result"]), unfurl_links=False)
                job.update(status="sent", delivery={"channel": channel, "ts": sent["ts"]})
                self.queue.write(key, job)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = TreasuryWorker({**(dotenv_values(args.env_file) if args.env_file else {}), **os.environ})
    while True:
        worker.tick()
        if args.once:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
