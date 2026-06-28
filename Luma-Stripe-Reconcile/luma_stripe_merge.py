#!/usr/bin/env python3
"""
luma_stripe_merge.py
====================
Pull Luma ticket sales + Stripe payments, merge them, and write a Xero
reconciliation worksheet (Sales detail + Payout summary).

Because Luma processes card payments through your own Stripe account, the same
sale appears in both systems. This script joins them so each ticket sale is
linked to its Stripe charge and rolled up into the Stripe *payout* that lands in
your bank — which is the line you actually reconcile in Xero.

Usage
-----
  # See the output shape with realistic fake data (no keys needed):
  python luma_stripe_merge.py --mock --out sample_reconciliation.xlsx

  # Real run (reads keys from environment / .env):
  python luma_stripe_merge.py --month 2026-06 --out june_2026.xlsx

Auth (real run)
---------------
  LUMA_API_KEY     Luma calendar/org API key (requires Luma Plus)
  STRIPE_API_KEY   Stripe restricted key, read-only: Charges, Balance
                   transactions, Payouts, Customers
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import defaultdict

LUMA_BASE = "https://public-api.luma.com/v1"
STRIPE_BASE = "https://api.stripe.com/v1"


def _money(x) -> float:
    return round(float(x or 0), 2)


def _load_dotenv() -> None:
    """Minimal .env loader so you don't need python-dotenv."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _month_bounds(month: str) -> tuple[int, int]:
    """'YYYY-MM' -> (start_unix, end_unix) covering that calendar month (UTC)."""
    y, m = (int(p) for p in month.split("-"))
    start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=dt.timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def _luma_get(path: str, params: dict | None = None) -> dict:
    import requests

    key = os.environ.get("LUMA_API_KEY")
    if not key:
        sys.exit("LUMA_API_KEY is not set (add it to .env). Or run with --mock.")
    r = requests.get(
        f"{LUMA_BASE}{path}",
        headers={"x-luma-api-key": key, "accept": "application/json"},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _luma_paginate(path: str, params: dict) -> list[dict]:
    """Walk Luma cursor pagination, returning all `entries`."""
    out, cursor = [], None
    while True:
        p = dict(params)
        if cursor:
            p["pagination_cursor"] = cursor
        data = _luma_get(path, p)
        out.extend(data.get("entries", data.get("data", [])))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor") or data.get("pagination_cursor")
        if not cursor:
            break
    return out


def fetch_luma_sales(start_unix: int, end_unix: int) -> list[dict]:
    """Return one dict per ticket sale within [start, end)."""
    sales: list[dict] = []
    events = _luma_paginate("/calendar/list-events", {})
    for ev in events:
        entry = ev.get("event", ev)
        event_id = entry.get("api_id") or entry.get("event_api_id") or entry.get("id")
        event_name = entry.get("name", "")
        guests = _luma_paginate("/event/get-guests", {"event_api_id": event_id})
        for g in guests:
            guest = g.get("guest", g)
            email = guest.get("email", "")
            name = guest.get("name") or guest.get("user_name") or ""
            detail = _luma_get(
                "/event/get-guest",
                {"event_api_id": event_id, "guest_api_id": guest.get("api_id")},
            ).get("guest", {})
            for order in detail.get("event_ticket_orders", []) or []:
                ts = order.get("created_at") or guest.get("registered_at")
                when = _parse_iso(ts)
                if when is None or not (start_unix <= when.timestamp() < end_unix):
                    continue
                sales.append(
                    {
                        "registered_at": when.isoformat(),
                        "event_id": event_id,
                        "event_name": event_name,
                        "ticket_type": order.get("ticket_type_name", ""),
                        "buyer_name": name,
                        "buyer_email": email,
                        "gross": _money(order.get("amount") or order.get("amount_total")),
                        "currency": (order.get("currency") or "AUD").upper(),
                        "coupon": (order.get("coupon_info") or {}).get("code", ""),
                        "luma_order_id": order.get("api_id", ""),
                    }
                )
    return sales


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _stripe_get(path: str, params: dict | None = None) -> dict:
    import requests

    key = os.environ.get("STRIPE_API_KEY")
    if not key:
        sys.exit("STRIPE_API_KEY is not set (add it to .env). Or run with --mock.")
    r = requests.get(
        f"{STRIPE_BASE}{path}",
        auth=(key, ""),
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _stripe_list(path: str, params: dict) -> list[dict]:
    out, starting_after = [], None
    while True:
        p = dict(params, limit=100)
        if starting_after:
            p["starting_after"] = starting_after
        data = _stripe_get(path, p)
        rows = data.get("data", [])
        out.extend(rows)
        if not data.get("has_more") or not rows:
            break
        starting_after = rows[-1]["id"]
    return out


def fetch_stripe_charges(start_unix: int, end_unix: int) -> list[dict]:
    """Charges grouped by the payout that settled them."""
    payouts = _stripe_list(
        "/payouts",
        {"arrival_date[gte]": start_unix, "arrival_date[lt]": end_unix},
    )
    charges = []
    for po in payouts:
        txns = _stripe_list(
            "/balance_transactions",
            {"payout": po["id"], "expand[]": "data.source"},
        )
        for t in txns:
            if t.get("type") != "charge":
                continue
            src = t.get("source") or {}
            charges.append(
                {
                    "charge_id": src.get("id") or t["id"],
                    "created": dt.datetime.fromtimestamp(
                        t["created"], dt.timezone.utc
                    ).isoformat(),
                    "gross": _money(t["amount"] / 100),
                    "stripe_fee": _money(t["fee"] / 100),
                    "net": _money(t["net"] / 100),
                    "currency": t["currency"].upper(),
                    "email": (src.get("billing_details") or {}).get("email", ""),
                    "description": src.get("description") or t.get("description") or "",
                    "payout_id": po["id"],
                    "payout_arrival": dt.datetime.fromtimestamp(
                        po["arrival_date"], dt.timezone.utc
                    ).date().isoformat(),
                }
            )
    return charges


def merge(luma_sales: list[dict], stripe_charges: list[dict]) -> list[dict]:
    """One row per ticket sale, linked to its Stripe charge + payout."""
    by_email_amt: dict[tuple, list[dict]] = defaultdict(list)
    for c in stripe_charges:
        by_email_amt[(c["email"].lower(), c["gross"])].append(c)

    rows = []
    for s in luma_sales:
        match = None
        for c in stripe_charges:
            if s["luma_order_id"] and s["luma_order_id"] in (c["description"] or ""):
                match = c
                break
        if not match:
            cands = by_email_amt.get((s["buyer_email"].lower(), s["gross"]), [])
            match = cands[0] if cands else None

        luma_fee = (
            _money(s["gross"] - match["net"] - match["stripe_fee"]) if match else 0.0
        )
        rows.append(
            {
                "date_bought": s["registered_at"],
                "event": s["event_name"],
                "ticket_type": s["ticket_type"],
                "buyer": s["buyer_email"] or s["buyer_name"],
                "gross": s["gross"],
                "luma_fee": luma_fee,
                "stripe_fee": match["stripe_fee"] if match else 0.0,
                "net": match["net"] if match else s["gross"],
                "currency": s["currency"],
                "stripe_charge_id": match["charge_id"] if match else "UNMATCHED",
                "payout_id": match["payout_id"] if match else "UNMATCHED",
                "payout_arrival": match["payout_arrival"] if match else "",
            }
        )
    return rows


def payout_summary(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        po = r["payout_id"]
        a = agg.setdefault(
            po,
            {
                "payout_id": po,
                "payout_arrival": r["payout_arrival"],
                "tickets": 0,
                "gross": 0.0,
                "luma_fee": 0.0,
                "stripe_fee": 0.0,
                "net": 0.0,
                "events": set(),
            },
        )
        a["tickets"] += 1
        a["gross"] = _money(a["gross"] + r["gross"])
        a["luma_fee"] = _money(a["luma_fee"] + r["luma_fee"])
        a["stripe_fee"] = _money(a["stripe_fee"] + r["stripe_fee"])
        a["net"] = _money(a["net"] + r["net"])
        a["events"].add(r["event"])
    out = []
    for a in agg.values():
        a["events"] = ", ".join(sorted(a["events"]))
        out.append(a)
    return sorted(out, key=lambda x: x["payout_arrival"])


def mock_rows() -> list[dict]:
    raw = [
        ("2026-06-03T09:12:00+00:00", "MLAI Workshop — June", "General",     "alice@example.com", 50.00, 1.15, "ch_3Na001", "po_06051", "2026-06-05"),
        ("2026-06-03T14:40:00+00:00", "MLAI Workshop — June", "General",     "bob@example.com",   50.00, 1.15, "ch_3Na002", "po_06051", "2026-06-05"),
        ("2026-06-04T19:05:00+00:00", "MLAI Workshop — June", "VIP",         "carol@example.com", 120.00, 2.34, "ch_3Na003", "po_06051", "2026-06-05"),
        ("2026-06-10T11:00:00+00:00", "MLAI Studio Mixer",    "Entry",       "dan@example.com",   30.00, 0.81, "ch_3Na004", "po_06121", "2026-06-12"),
        ("2026-06-11T16:22:00+00:00", "MLAI Studio Mixer",    "Entry",       "erin@example.com",  30.00, 0.81, "ch_3Na005", "po_06121", "2026-06-12"),
        ("2026-06-11T16:25:00+00:00", "MLAI Studio Mixer",    "Entry+Drink", "frank@example.com", 45.00, 1.07, "ch_3Na006", "po_06121", "2026-06-12"),
    ]
    rows = []
    for d, ev, tt, buyer, gross, sfee, ch, po, arr in raw:
        luma_fee = _money(gross * 0.02)
        net = _money(gross - luma_fee - sfee)
        rows.append(
            {
                "date_bought": d,
                "event": ev,
                "ticket_type": tt,
                "buyer": buyer,
                "gross": gross,
                "luma_fee": luma_fee,
                "stripe_fee": sfee,
                "net": net,
                "currency": "AUD",
                "stripe_charge_id": ch,
                "payout_id": po,
                "payout_arrival": arr,
            }
        )
    return rows


SALES_COLS = [
    ("date_bought", "Date bought (UTC)"),
    ("event", "Event"),
    ("ticket_type", "Ticket type"),
    ("buyer", "Buyer"),
    ("gross", "Gross"),
    ("luma_fee", "Luma fee"),
    ("stripe_fee", "Stripe fee"),
    ("net", "Net"),
    ("currency", "Ccy"),
    ("stripe_charge_id", "Stripe charge"),
    ("payout_id", "Payout"),
    ("payout_arrival", "Payout date"),
]
PAYOUT_COLS = [
    ("payout_id", "Payout"),
    ("payout_arrival", "Arrival date"),
    ("tickets", "# Tickets"),
    ("gross", "Gross"),
    ("luma_fee", "Luma fee"),
    ("stripe_fee", "Stripe fee"),
    ("net", "Net (bank deposit)"),
    ("events", "Events"),
]


def write_xlsx(rows: list[dict], path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    money_cols = {"gross", "luma_fee", "stripe_fee", "net"}
    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)

    wb = Workbook()

    def render(ws, cols, data, total_keys):
        ws.append([label for _, label in cols])
        for c in range(1, len(cols) + 1):
            cell = ws.cell(1, c)
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(horizontal="center")
        for r in data:
            ws.append([r.get(k, "") for k, _ in cols])
        trow = ws.max_row + 1
        ws.cell(trow, 1, "TOTAL").font = bold
        for idx, (k, _) in enumerate(cols, start=1):
            if k in total_keys:
                col = get_column_letter(idx)
                ws.cell(trow, idx, f"=SUM({col}2:{col}{ws.max_row-1})").font = bold
        for idx, (k, _) in enumerate(cols, start=1):
            col = get_column_letter(idx)
            width = max(len(str(cols[idx-1][1])), *(len(str(r.get(k, ""))) for r in data)) if data else len(cols[idx-1][1])
            ws.column_dimensions[col].width = min(max(width + 3, 11), 40)
            if k in money_cols or k == "net":
                for row in range(2, ws.max_row + 1):
                    ws.cell(row, idx).number_format = '#,##0.00'
        ws.freeze_panes = "A2"

    ws1 = wb.active
    ws1.title = "Sales detail"
    render(ws1, SALES_COLS, rows, {"gross", "luma_fee", "stripe_fee", "net"})

    ws2 = wb.create_sheet("Payout summary")
    render(ws2, PAYOUT_COLS, payout_summary(rows), {"tickets", "gross", "luma_fee", "stripe_fee", "net"})

    ws3 = wb.create_sheet("About")
    notes = [
        "MLAI — Luma → Stripe reconciliation worksheet",
        "",
        "Sales detail : one row per ticket sale, linked to its Stripe charge and payout.",
        "Payout summary: one row per Stripe payout = the lump sum that lands in your bank.",
        "",
        "Reconcile in Xero against the 'Net (bank deposit)' column on Payout summary —",
        "each payout matches one transfer out of your Stripe clearing account.",
        "",
        "UNMATCHED in a Stripe column = a Luma sale with no matching charge found",
        "(check email/amount, refunds, or a cross-month payout).",
    ]
    for n in notes:
        ws3.append([n])
    ws3.column_dimensions["A"].width = 80

    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Luma + Stripe -> Xero reconciliation worksheet")
    ap.add_argument("--mock", action="store_true", help="use built-in sample data (no API keys)")
    ap.add_argument("--month", help="YYYY-MM to pull (real run). Default: last month")
    ap.add_argument("--out", default="reconciliation.xlsx", help="output .xlsx path")
    args = ap.parse_args()

    if args.mock:
        rows = mock_rows()
    else:
        _load_dotenv()
        month = args.month or (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        start, end = _month_bounds(month)
        print(f"Pulling Luma sales for {month} ...")
        luma = fetch_luma_sales(start, end)
        print(f"  {len(luma)} Luma ticket sales")
        print(f"Pulling Stripe charges/payouts for {month} ...")
        stripe = fetch_stripe_charges(start, end)
        print(f"  {len(stripe)} Stripe charges")
        rows = merge(luma, stripe)

    write_xlsx(rows, args.out)
    n_un = sum(r["payout_id"] == "UNMATCHED" for r in rows)
    print(f"Wrote {args.out}: {len(rows)} sales, {len(payout_summary(rows))} payouts"
          + (f", {n_un} UNMATCHED" if n_un else ""))


if __name__ == "__main__":
    main()
