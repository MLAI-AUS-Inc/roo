"""Member invoices derived only from the existing quarter-hour allocation engine."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import io
from pathlib import Path
from xml.sax.saxutils import escape

from .timesheets import TimesheetError, fingerprint, flag, timestamp

FIRST_INVOICE = date(2026, 10, 23)
RATE_CENTS = 6500


@dataclass(frozen=True)
class InvoiceConfig:
    enabled: bool = False
    scheduled: bool = False
    queue: Path = Path('/app/timesheets/invoice-queue')

    @classmethod
    def from_env(cls, env, timesheets):
        enabled = flag(env, 'STUDIO_INVOICES_ENABLED')
        scheduled = flag(env, 'STUDIO_INVOICES_SCHEDULE_ENABLED')
        if enabled and (not timesheets.enabled or timesheets.source != 'linear'):
            raise TimesheetError('invoices_require_linear_timesheet_worker')
        timesheets.cutoff(FIRST_INVOICE)
        return cls(enabled, scheduled, Path(env.get('STUDIO_INVOICE_QUEUE_DIR', '/app/timesheets/invoice-queue')))


def current_period(config, now):
    return config.latest(now) + timedelta(days=14)


def validate_period(config, period):
    try:
        end = config.cutoff(period)
    except (ValueError, TypeError) as exc:
        raise TimesheetError('not_a_payment_cutoff') from exc
    if end.date() < FIRST_INVOICE:
        raise TimesheetError('invoice_before_rollout')
    return end


def draft_at(config, period):
    return validate_period(config, period) - timedelta(days=1) + timedelta(hours=5)


def build_invoice(config, report, builder_id, period):
    end = validate_period(config, period)
    if report.get('source') != 'linear' or not report.get('coverage_verified'):
        raise TimesheetError('invoice_source_unverified')
    if report['period'] != end.date().isoformat():
        raise TimesheetError('invoice_period_mismatch')
    builder = report['builders'].get(builder_id)
    if builder is None or config.builders.get(builder_id, {}).get('slack_user_id') != builder['slack_user_id']:
        raise TimesheetError('invoice_owner_changed')
    rows = deepcopy([r for r in report['rows'] if r['builder_id'] == builder_id])
    if any(type(row['units']) is not int or row['slack_user_id'] != builder['slack_user_id'] for row in rows):
        raise TimesheetError('invalid_invoice_row')
    units = sum(r['units'] for r in rows)
    if units <= 0:
        return None
    identity = {'team': config.team, 'organization': config.organization,
                'builder_id': builder_id, 'period': period}
    return {**identity, 'version': 1, 'slack_user_id': builder['slack_user_id'],
            'builder': builder['name'], 'reference': 'ROO-' + fingerprint(identity)[:16].upper(),
            'draft': report['draft'], 'start': report['start'], 'end': report['end'],
            'generated_at': report['generated_at'], 'source_sha256': fingerprint(report),
            'rubric': report['rubric'], 'rows': rows, 'units': units, 'rate_cents': RATE_CENTS,
            'total_cents': units * 1625, 'currency': 'AUD', 'complete': report['complete']}


def money(cents):
    return f'${Decimal(cents) / 100:,.2f}'


def hours(units):
    return format(Decimal(units) / 4, 'f').rstrip('0').rstrip('.') if units % 4 else str(units // 4)


def _text(value):
    # Standard PDF fonts cover Latin-1. Keep other codepoints explicit rather
    # than silently drawing missing-glyph boxes; source JSON retains Unicode.
    return str(value).replace('\u2013', '-').replace('\u2014', '-').encode('latin-1', 'backslashreplace').decode('latin-1')


def render_pdf(invoice):
    """AcroForm personal fields only; money/source text is ordinary page content."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

    output = io.BytesIO()
    width, height = A4
    styles = getSampleStyleSheet()
    styles['Normal'].fontSize = 9
    styles['Normal'].leading = 12
    ink = colors.HexColor('#17324d')
    tint = colors.HexColor('#eef4f8')

    def header(canvas, doc):
        canvas.setFillColor(ink)
        canvas.setFont('Helvetica-Bold', 23)
        canvas.drawString(42, height - 54, 'Invoice')
        canvas.setFont('Helvetica-Bold', 11)
        canvas.drawRightString(width - 42, height - 52, 'DRAFT' if invoice['draft'] else 'FINAL')
        canvas.setFont('Helvetica', 9)
        canvas.drawString(42, height - 75, 'Bill to: MLAI')
        canvas.drawString(42, height - 91, f"Payment fortnight ending {invoice['period']} | AUD | {money(invoice['rate_cents'])}/hour")
        canvas.drawString(42, height - 107, 'Internal reference: ' + invoice['reference'])
        canvas.drawString(42, height - 123, 'Complete the blue fields locally, then save a copy for issuing.')
        if doc.page == 1:
            fields = [('business_name', 'Business / supplier name'), ('contact_name', 'Contact name'),
                      ('email', 'Email / phone'), ('address', 'Business address'),
                      ('abn', 'ABN'), ('invoice_number', 'Your invoice number'),
                      ('bsb', 'BSB'), ('account_number', 'Account number'),
                      ('account_name', 'Account name'), ('invoice_date', 'Invoice date'),
                      ('due_date', 'Due date'), ('payment_notes', 'Payment notes')]
            for i, (name, label) in enumerate(fields):
                x = 42 + (i % 2) * 265
                y = height - 174 - (i // 2) * 43
                canvas.setFont('Helvetica', 8)
                canvas.drawString(x, y + 25, label)
                canvas.acroForm.textfield(name=name, tooltip=label, x=x, y=y,
                    width=245, height=21, value='', fontName='Helvetica', fontSize=10,
                    borderWidth=.5, borderColor=colors.HexColor('#93afc4'),
                    fillColor=tint, textColor=ink, forceBorder=True, maxlen=200)
        canvas.setFont('Helvetica', 8)
        canvas.drawString(42, 25, invoice['reference'] + ' | ' + ('Draft estimate' if invoice['draft'] else 'Sealed timesheet snapshot'))
        canvas.drawRightString(width - 42, 25, f'Page {doc.page}')

    def continuation(canvas, doc):
        header(canvas, doc)

    def paragraph(value):
        return Paragraph(escape(_text(value)).replace('\n', '<br/>'), styles['Normal'])

    rows = [[paragraph(x) for x in ['Work item', 'Description / project', 'Size', 'Hours', 'Amount']]]
    for row in invoice['rows']:
        description = row['title'] + '\n' + row['project']
        if row['kind'] == 'correction':
            description += '\nAdjustment: ' + row['reason'] + '\n' + row['reference']
        rows.append([paragraph(row['identifier']), paragraph(description),
                     paragraph(row['size'] if row['kind'] == 'ticket' else '-'),
                     paragraph(hours(row['units'])), paragraph(money(row['units'] * 1625))])
    table = Table(rows, colWidths=[63, 277, 35, 48, 88], repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), tint),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('LINEBELOW', (0, 0), (-1, -1), .3, colors.lightgrey)]))
    note = ('Draft estimate as of ' + invoice['end'] + '. The Friday final supersedes this draft.'
            if invoice['draft'] else 'This final supersedes any draft for this fortnight. Adjustments appear in a later fortnight.')
    if not invoice['complete']:
        note += ' Some source work needs review and is excluded from these amounts.'
    story = [Spacer(1, 283), table, Spacer(1, 16), KeepTogether([
        Paragraph(f"<b>Total: {money(invoice['total_cents'])} AUD</b> ({hours(invoice['units'])} hours)", styles['Normal']),
        Spacer(1, 10), paragraph(note)])]
    SimpleDocTemplate(output, pagesize=A4, leftMargin=42, rightMargin=42,
                      topMargin=143, bottomMargin=48, title='Invoice', author='Roo Studio').build(
                          story, onFirstPage=header, onLaterPages=continuation)
    return output.getvalue()
