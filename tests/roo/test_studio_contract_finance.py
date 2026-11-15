"""A paid monthly invoice settles cash without granting the allowance twice."""

from copy import deepcopy
import base64
from unittest.mock import Mock

import pytest

from roo.studio_contract_finance import validate_cash_policy, validate_cash_result
from roo.timesheets import TimesheetError
from tests.roo.test_studio_contract_cycles import NOW, contract, eight_hours, request
from tests.roo.test_studio_reports import setup

CONTACT = "00000000-0000-4000-8000-000000000001"
POLICY = {"xero_contact_id": CONTACT, "monthly_fee_net_cents": 500000}


def result():
    return {
        "contact_id": CONTACT,
        "checked_at": NOW.isoformat(),
        "finance_checked_at": NOW.isoformat(),
        "status": "ready",
        "missing": [],
        "ledger": {
            "invoices": [
                {
                    "id": "i",
                    "number": "INV-1",
                    "date": "2026-10-16",
                    "amount_cents": 550000,
                    "paid_cents": 550000,
                    "tax_cents": 50000,
                    "net_cents": 500000,
                    "status": "paid",
                }
            ],
            "payments": [
                {
                    "id": "p",
                    "date": "2026-10-22",
                    "amount_cents": 550000,
                    "allocations": [{"invoice_id": "i", "amount_cents": 550000}],
                }
            ],
        },
    }


def test_combined_report_uses_one_allowance_and_revalidates_cash_at_delivery(contract):
    service = contract.service
    service.balance_enabled = True
    client = contract.clients["UOTHER"]
    client["contract_cash"] = deepcopy(POLICY)
    validate_cash_policy(client)
    source = validate_cash_result(result(), POLICY, NOW)
    service.refresh = Mock()
    service.refresh.prepare.return_value = ("receipt", source, {})
    service.refresh.frozen.return_value = source
    contract.api.collect.return_value = eight_hours()
    req = {**request(month="2026-09", months=3), "report_kind": "client_balance"}
    report = service.report_for_request(req)
    assert report["finance_owner"] == "UOTHER"
    assert report["contract_cash_view"]["paid"] == 550000
    assert report["contract_cash_view"]["outstanding"] == 0
    assert report["contract_allowance"]["cycles"][0]["remaining_hours"] == "32"
    (chart,) = [p for p in report["parts"] if p.get("filename") == "studio-contract-payments.png"]
    assert base64.b64decode(chart["content"]).startswith(b"\x89PNG")
    service.deliver_parts = Mock(return_value=True)
    assert service.deliver_request(report, req, "key", NOW)
    service.refresh.frozen.return_value = {"changed": True}
    with pytest.raises(TimesheetError, match="client_balance_source_changed"):
        service.deliver_request(report, req, "key", NOW)
    assert service.deliver_parts.call_count == 1
    del client["contract_cash"]
    with pytest.raises(TimesheetError, match="client_access_changed"):
        service.deliver_request(report, req, "key", NOW)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(contact_id="unrelated"),
        lambda r: r["ledger"]["payments"].append(deepcopy(r["ledger"]["payments"][0])),
        lambda r: r["ledger"]["invoices"][0].update(net_cents=550000),
        lambda r: r.update(finance_checked_at="2026-10-10T00:00:00Z"),
    ],
)
def test_wrong_contact_unbalanced_or_stale_source_is_rejected(change):
    data = result()
    change(data)
    with pytest.raises(TimesheetError):
        validate_cash_result(data, POLICY, NOW)
