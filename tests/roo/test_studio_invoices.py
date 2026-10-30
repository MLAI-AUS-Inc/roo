"""Offline contract tests: hand-derived money, local periods and actual AcroForms."""
from copy import deepcopy
from datetime import timedelta
import io

import pytest
from pypdf import PdfReader, PdfWriter

from tests.roo.test_timesheets import config, USER, OTHER, ticket, item
from roo.timesheets import build_report, empty_ledger, timestamp, TimesheetError


def report_for(config, *, draft=False, rows=1, size='Small (S)'):
    when = timestamp('2026-10-22T17:00:00+11:00') if draft else timestamp('2026-10-23T12:00:00+11:00')
    dataset = [item(ticket(n, size, completedAt='2026-10-20T01:00:00Z',
                           updatedAt='2026-10-20T01:00:00Z')) for n in range(rows)]
    return build_report(config, when.astimezone(config.tz), dataset, empty_ledger(), draft=draft)


@pytest.mark.parametrize('size,units,cents', [
    ('Extra Small (XS)', 1, 1625), ('Small (S)', 4, 6500),
    ('Medium (M)', 8, 13000), ('Large (L)', 12, 19500), ('Extra Large (XL)', 20, 32500),
])
def test_member_amounts_and_source_rows(config, size, units, cents):
    from roo.studio_invoices import build_invoice
    report = report_for(config, size=size)
    before = deepcopy(report)
    report['rows'].append(dict(report['rows'][0], builder_id=OTHER, title='Other private work'))
    invoice = build_invoice(config, report, USER, '2026-10-23')
    assert (invoice['units'], invoice['total_cents'], invoice['rate_cents']) == (units, cents, 6500)
    assert len(invoice['rows']) == 1
    assert invoice['rows'][0]['source_issue_id'] == before['rows'][0]['source_issue_id']
    assert 'Other private work' not in str(invoice)
    assert invoice['period'] == '2026-10-23'


def test_nonpayable_corrections_do_not_generate_empty_pdf(config):
    from roo.studio_invoices import build_invoice
    report = report_for(config)
    row = dict(report['rows'][0], kind='correction', units=-4, reference='reversal')
    report['rows'].append(row)
    assert build_invoice(config, report, USER, '2026-10-23') is None
    assert build_invoice(config, report, OTHER, '2026-10-23') is None


@pytest.mark.parametrize('at,want', [
    ('2026-10-22T16:59:59+11:00', '2026-10-23'),
    ('2026-10-23T11:59:59+11:00', '2026-10-23'),
    ('2026-10-23T12:00:00+11:00', '2026-11-06'),
    ('2027-04-02T11:59:59+11:00', '2027-04-09'),
    ('2027-04-09T11:59:59+10:00', '2027-04-09'),
])
def test_current_fortnight_and_dst(config, at, want):
    from roo.studio_invoices import current_period, draft_at
    assert current_period(config, timestamp(at)).date().isoformat() == want
    assert draft_at(config, '2026-10-23').isoformat() == '2026-10-22T17:00:00+11:00'
    assert draft_at(config, '2027-04-09').utcoffset() == timedelta(hours=10)


def test_rollout_excludes_earlier_invoices(config):
    from roo.studio_invoices import validate_period
    for value in ('2026-10-09', '2026-10-22', '2026-10-30', '../secret'):
        with pytest.raises(TimesheetError):
            validate_period(config, value)
    assert validate_period(config, '2026-10-23').date().isoformat() == '2026-10-23'


@pytest.mark.parametrize('generated_at,issue_date', [
    ('2026-10-22T14:30:00+00:00', '23 October 2026'),
    ('2027-04-08T14:30:00+00:00', '09 April 2027'),
])
def test_pdf_fields_survive_save_reopen_and_financials_are_static(config, generated_at, issue_date):
    from roo.studio_invoices import build_invoice, render_pdf
    invoice = build_invoice(config, report_for(config, draft=True), USER, '2026-10-23')
    invoice['generated_at'] = generated_at
    pdf = render_pdf(invoice)
    reader = PdfReader(io.BytesIO(pdf))
    fields = reader.get_fields()
    expected = {'business_name', 'contact_name', 'email', 'address', 'abn', 'bsb',
                'account_name', 'account_number', 'invoice_number', 'invoice_date'}
    assert set(fields) == expected
    assert fields['invoice_date']['/V'] == issue_date
    assert all(field.get('/V', '') == '' for name, field in fields.items() if name != 'invoice_date')
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'Invoice' in text and 'DRAFT' in text and 'MLAI' in text
    assert 'Bill to: MLAI | ABN: 94 807 394 137' in text
    assert 'Send your completed final invoice to treasurer@mlai.au.' in text
    assert 'Your ABN' in text
    assert 'Issue date' in text
    assert 'Due date' not in text and 'Payment notes' not in text
    assert '$65.00' in text and 'GST' not in text and 'Tax Invoice' not in text
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    values = {'abn': '12345678901', 'bsb': '123-456', 'account_number': '987654321',
              'business_name': 'Example Builder Pty Ltd', 'invoice_number': 'INV-1001'}
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    output = io.BytesIO()
    writer.write(output)
    reopened = PdfReader(io.BytesIO(output.getvalue()))
    assert {name: reopened.get_fields()[name]['/V'] for name in values} == values
    assert reopened.get_fields()['invoice_date']['/V'] == issue_date
    widgets = [a.get_object() for page in reopened.pages for a in page.get('/Annots', [])
               if a.get_object().get('/Subtype') == '/Widget']
    assert {w['/T'] for w in widgets} == expected
    for widget in widgets:
        assert widget['/V'] == reopened.get_fields()[widget['/T']]['/V']
        assert widget['/AP']['/N'].get_object().get_data()


def test_long_work_rows_are_not_truncated_and_form_is_present_only_once(config):
    from roo.studio_invoices import build_invoice, render_pdf
    report = report_for(config, rows=50)
    for row in report['rows']:
        row['title'] = 'Long work description ' * 15 + 'end-of-description'
    reader = PdfReader(io.BytesIO(render_pdf(build_invoice(config, report, USER, '2026-10-23'))))
    text = '\n'.join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) > 1
    assert text.count('end-of-description') == 50
    assert '$3,250.00' in text
    assert len(reader.get_fields()) == 10
