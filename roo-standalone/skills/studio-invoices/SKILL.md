---
name: studio-invoices
description: Member invoices to MLAI from approved Studio work
requires_auth: true
routing:
  use_when: How to get or fill your own builder invoice PDF for MLAI.
  avoid_when: Client purchased hours, timesheets for Sam, tax advice or editing paid amounts.
  examples:
    - {text: "How do I get my Studio invoice?", action: help}
    - {text: "Can I put my ABN and bank details in the invoice?", action: help}
    - {text: "When does Roo send my final invoice?", action: help}
  negative_examples:
    - {text: "Show my client's purchased hours balance", instead: studio-client-balance}
actions:
  - name: help
    description: Explain exact private invoice commands and locally editable PDF fields.
    params: {}
---

# Studio invoices

Explain `@Roo invoice` in an allowed channel or `invoice` in a DM for the
current-fortnight draft. `invoice latest` retrieves the most recent payable
final. `invoice YYYY-MM-DD` selects a payment Friday from 23 October 2026;
the current open period returns a draft. Signed exact commands queue work for
the private timesheet worker. Never infer a requester, select another member,
calculate a financial total yourself, or collect ABN/bank details in chat.

Completed Linear work uses XS=.25h, S=1h, M=2h, L=3h, XL=5h at $65/hour
with no additional GST. Drafts arrive every second Thursday at 5pm Melbourne
time; Friday finals supersede them after noon. Business/contact details,
ABN, bank details and invoice metadata are editable locally in the PDF.
Work and amounts are fixed. Save the filled copy locally before issuing it.
