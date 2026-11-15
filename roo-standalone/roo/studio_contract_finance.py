"""Private cash and contract usage reporting for owners with a reviewed Linear policy."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import UUID

from .studio_client_balance import money
from .studio_contract_cycles import hours
from .studio_reports import TZ, resolve_period
from .timesheets import TimesheetError, timestamp


def validate_cash_policy(client):
    policy = client.get("contract_cash")
    if policy is None:
        return
    try:
        if (
            client.get("hours_source") != "linear"
            or not client.get("contract_terms")
            or set(policy) != {"xero_contact_id", "monthly_fee_net_cents"}
            or str(UUID(policy["xero_contact_id"])) != policy["xero_contact_id"]
            or type(policy["monthly_fee_net_cents"]) is not int
            or policy["monthly_fee_net_cents"] <= 0
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise TimesheetError("invalid_contract_cash_policy") from exc


def validate_cash_result(result, policy, cutoff):
    if result["contact_id"] != policy["xero_contact_id"]:
        raise TimesheetError("studio_source_scope_mismatch")
    if result["status"] == "waiting_data" and result.get("missing") and "ledger" not in result:
        return None
    checked = timestamp(result["checked_at"])
    as_of = timestamp(result["finance_checked_at"])
    if (
        not checked - timedelta(days=7) <= as_of <= checked
        or checked < timestamp(cutoff)
        or (result["status"], result["missing"]) not in [("ready", []), ("partial", ["finance_rate_limited"])]
    ):
        raise TimesheetError("studio_source_coverage_invalid")
    ledger = result["ledger"]
    invoices = {v["id"]: v for v in ledger["invoices"]}
    payments = ledger["payments"]

    def cents(n):
        return type(n) is int and n >= 0

    try:
        if len(invoices) != len(ledger["invoices"]) or len({p["id"] for p in payments}) != len(payments):
            raise ValueError
        sums = dict.fromkeys(invoices, 0)
        end = timestamp(cutoff).astimezone(TZ).date()
        for p in payments:
            seen = set()
            if not cents(p["amount_cents"]) or date.fromisoformat(p["date"]) > end or not p["allocations"]:
                raise ValueError
            for part in p["allocations"]:
                key, amount = part["invoice_id"], part["amount_cents"]
                if (
                    key in seen
                    or not cents(amount)
                    or not amount
                    or p["date"] < invoices[key]["date"]
                    or invoices[key]["status"] not in {"paid", "authorised"}
                ):
                    raise ValueError
                seen.add(key)
                sums[key] += amount
            if sum(v["amount_cents"] for v in p["allocations"]) != p["amount_cents"]:
                raise ValueError
        for key, inv in invoices.items():
            gross, net, tax, paid = (inv[k] for k in ("amount_cents", "net_cents", "tax_cents", "paid_cents"))
            if (
                not all(cents(v) for v in (gross, net, tax, paid))
                or net + tax != gross
                or not 0 <= paid <= gross
                or sums[key] != paid
                or date.fromisoformat(inv["date"]) > end
                or inv["status"] not in {"paid", "authorised", "draft", "voided"}
                or (inv["status"] == "paid" and paid != gross)
            ):
                raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise TimesheetError("invalid_contract_cash_ledger") from exc
    return {
        "ledger": ledger,
        "finance_checked_at": result["finance_checked_at"],
        "missing": result["missing"],
    }


def financial_view(report, client, source):
    _, start, next_period = resolve_period(report["selector"], report["generated_at"])
    end = min(next_period.date() - timedelta(days=1), timestamp(report["generated_at"]).astimezone(TZ).date())
    first, last = start.date().isoformat(), end.isoformat()
    checked_day = timestamp(source["finance_checked_at"]).astimezone(TZ).date().isoformat()
    ledger = source["ledger"]
    invoices = [v for v in ledger["invoices"] if v["status"] in {"paid", "authorised"} and v["date"] <= last]
    payments = [v for v in ledger["payments"] if v["date"] <= last]
    allocations = {v["id"]: 0 for v in invoices}
    for p in payments:
        for part in p["allocations"]:
            allocations[part["invoice_id"]] += part["amount_cents"]
    period_invoices = sorted((v for v in invoices if first <= v["date"]), key=lambda v: (v["date"], v["id"]))
    period_payments = sorted((p for p in payments if first <= p["date"]), key=lambda p: (p["date"], p["id"]))
    names = {v["id"]: v["number"] for v in invoices}
    return {
        "start": first,
        "end": last,
        "cash_through": min(last, checked_day),
        "checked_at": source["finance_checked_at"],
        "missing": source["missing"],
        "invoices": period_invoices,
        "payments": [
            {**p, "numbers": [names[v["invoice_id"]] for v in p["allocations"]]} for p in period_payments
        ],
        "billed": sum(v["amount_cents"] for v in period_invoices),
        "paid": sum(p["amount_cents"] for p in period_payments),
        "outstanding": sum(v["amount_cents"] - allocations[v["id"]] for v in invoices),
        "monthly_fee": client["contract_cash"]["monthly_fee_net_cents"],
        "base_hours": client["contract_terms"]["included_hours"],
    }


def cash_summary(view):
    msg = [f"*Xero invoices and payments · {view['start']} – {view['end']}*"]
    if view["cash_through"] >= view["start"]:
        msg.append(
            f"Verified through {view['cash_through']}: invoices {money(view['billed'])}, "
            f"received {money(view['paid'])}, outstanding {money(view['outstanding'])} (GST inclusive)."
        )
    else:
        msg.append("Invoice and payment activity for this period has not yet been verified.")
    msg.extend(
        f"• {v['date']} · invoice {v['number']} · {money(v['net_cents'])} + {money(v['tax_cents'])} GST."
        for v in view["invoices"]
    )
    msg.extend(
        f"• {p['date']} · received {money(p['amount_cents'])} against {', '.join(p['numbers'])}."
        for p in view["payments"]
    )
    checked = timestamp(view["checked_at"]).astimezone(TZ)
    msg.append(
        f"Xero cash verified {checked:%d %b %Y %H:%M %Z}."
        + (" Xero is rate-limited." if view["missing"] else "")
    )
    msg.append(
        f"The signed monthly package is {view['base_hours']}h for {money(view['monthly_fee'])} before GST. "
        "A payment settles an invoice. The monthly allowance starts on the contract anniversary."
    )
    return "\n".join(msg)


def render_chart(report, view):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    def day(s):
        return datetime.combine(date.fromisoformat(s), time.min, TZ)

    first, last = day(view["start"]), day(view["end"])
    cutoff = min(timestamp(report["generated_at"]).astimezone(TZ), last + timedelta(days=1))
    fig = Figure(figsize=(11, 8), dpi=145, facecolor="white")
    cash, allowance = fig.subplots(2, 1, sharex=True)
    fig.subplots_adjust(left=0.12, right=0.96, top=0.78, bottom=0.17, hspace=0.5)
    fig.text(0.08, 0.94, "Studio payments and contract usage", fontsize=22, weight="bold", color="#163342")
    fig.text(
        0.08,
        0.89,
        f"{report['client']}  ·  {first:%d %b %Y} – {last:%d %b %Y}",
        fontsize=13,
        color="#345262",
        parse_math=False,
    )
    cash_label = (
        f"Invoiced {money(view['billed'])}    Received {money(view['paid'])}    Outstanding {money(view['outstanding'])}"
        if view["cash_through"] >= view["start"]
        else "Cash activity for this period is awaiting verification."
    )
    fig.text(0.08, 0.84, cash_label, fontsize=12, color="#163342")
    for entries, name, color, dash in (
        (view["invoices"], "Invoiced", "#58687c", "--"),
        (view["payments"], "Received", "#008576", "-"),
    ):
        xs, ys, total = [first], [0], 0
        for e in entries:
            total += e["amount_cents"]
            xs.append(day(e["date"]))
            ys.append(total / 100)
        finance_end = min(cutoff, timestamp(view["checked_at"]).astimezone(TZ))
        if finance_end >= first:
            xs.append(finance_end)
            ys.append(total / 100)
        else:
            xs.clear()
            ys.clear()
        cash.step(xs, ys, where="post", label=name, color=color, linestyle=dash, linewidth=2.5)
        cash.scatter(xs[1:-1], ys[1:-1], color=color, s=25)
    if finance_end < cutoff:
        cash.axvspan(max(first, finance_end), cutoff, color="#e9eef1", label="After Xero check")
    cash.set_title("Cumulative invoices and receipts in the selected period", loc="left", fontsize=12)
    cash.set_ylabel("A$ incl. GST")
    cash.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
    cash.legend(loc="upper left", frameon=False, ncol=2)
    contract = report["contract_allowance"]
    for c in contract["cycles"]:
        points = c["points"]
        if c["complete"] and points:
            allowance.step(
                [timestamp(p["at"]) for p in points],
                [float(p["hours"]) for p in points],
                where="post",
                color="#20738b",
                linewidth=2.6,
            )
        else:
            allowance.axvspan(timestamp(c["start"]), timestamp(c["as_of"]), color="#e1e7ec")
    begins = day(contract["commencement_date"])
    if begins > first:
        allowance.axvspan(first, min(begins, cutoff), color="#e9eef1")
    allowance.set_title("Monthly allowance remaining after completed Linear tasks", loc="left", fontsize=12)
    allowance.set_ylabel("Estimated hours")
    allowance.set_xlim(first, max(first + timedelta(days=1), cutoff))
    if allowance.get_ylim()[0] > 0:
        allowance.set_ylim(bottom=0)
    locator = AutoDateLocator(minticks=4, maxticks=8, tz=TZ)
    allowance.xaxis.set_major_locator(locator)
    allowance.xaxis.set_major_formatter(ConciseDateFormatter(locator, tz=TZ))
    for ax in (cash, allowance):
        ax.grid(axis="y", color="#e1e8ec")
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    if contract["cycles"]:
        c = contract["cycles"][-1]
        if c["complete"]:
            rate = Decimal(view["monthly_fee"]) / Decimal(str(view["base_hours"]))
            used = Decimal(c["used_hours"])
            fig.text(
                0.08,
                0.095,
                f"Latest cycle in view: {hours(used)}h used · {c['remaining_hours']}h remaining · "
                f"{money(used * rate)} of work at the package equivalent, before GST.",
                fontsize=10,
                color="#163342",
            )
        else:
            fig.text(
                0.08,
                0.095,
                "Incomplete Linear evidence. The affected contract balance is unconfirmed.",
                fontsize=10,
            )
    stamp = timestamp(view["checked_at"]).astimezone(TZ)
    fig.text(
        0.08,
        0.055,
        f"Contract starts {begins:%d %b %Y}. Task sizes estimate usage. Xero checked {stamp:%d %b %Y %H:%M %Z}."
        + (" Rate-limit cache in use." if view["missing"] else ""),
        fontsize=9,
        color="#425d69",
    )
    data = BytesIO()
    FigureCanvasAgg(fig).print_png(data)
    return data.getvalue()
