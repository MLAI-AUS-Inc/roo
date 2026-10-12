# Invoice implementation handoff - 12 October 2026

Branch: `codex/roo-studio-invoices`, based on Roo main `b0bd2b5`.
Runtime is opt-in and has not been deployed or enabled. No live Slack deliveries
or Linear writes were performed. Follow [the operating guide](studio-invoices.md).

Implemented the approved size/hour/$65 rate, signed own-invoice commands,
private PDF generation, October 22/23 rollout, Melbourne fortnight schedule,
sealed finals, existing corrections and persistent artifact/delivery recovery.

## Verification

- Baseline existing timesheet/ingress: 64 passed.
- Final bare repository pytest: **2,476 passed**, 23.60 seconds.
- Final CI split test step: **1,846 passed across 11 isolated runs**;
  worker/report group includes 537 tests. Compileall passed in that step.
- Both Public Roo invoice queue override and private worker Compose
  configurations passed `docker compose config --quiet`, using example values.
- Actual PDF field tree, widgets, values and appearance streams survived
  save/reopen. Single-page and nine-page rendered samples were inspected.
- Preview's UI automation could not finish opening the sample; Acrobat Reader
  is not installed. **Actual desktop-reader save/reopen validation is pending
  and remains a prerequisite before production rollout.**

One fresh **gpt-6-astra** whole-branch review examined the implementation.
No critical findings. Two important findings were fixed in one regression pass:

1. A partially delivered `invoice latest` could change invoice when retried
   after the next cutoff. The private request selection now pins its original
   period before transport. Rate-limited and uncertain/recovered deliveries
   were both reproduced failing, then verified passing across a fortnight.
2. A stale timestamp could permit draft delivery after a slow Slack call
   crossed Friday noon. Deadline checks now run before each outgoing operation,
   including the upload protocol's final sharing call. Tests for delayed
   recipient verification, DM opening, message posting and upload stages were
   reproduced failing and verified passing. Unsent parts expire; sent or
   ambiguous parts retain their durable receipt state.

These seven regression cases passed alongside the full suite. No second
whole-branch review was dispatched.

## Rulings recorded during execution

1. Use an isolated worktree from the hydrated Roo repository after the native
   worktree tool targeted the unrelated parent repository. Current main was
   `b0bd2b5`. Cost if wrong: move the checkout; no runtime impact.
2. Initially choose Roo's existing PyMuPDF dependency for AcroForms instead of
   another production dependency. Cost if wrong: replace the generator if
   desktop interoperability fails. This provisional choice was superseded.
3. Replace that choice with ReportLab because PyMuPDF setup initially exceeded
   writable disk space while ReportLab was available. Native AcroForm field
   trees passed save/reopen validation. Cost if wrong: retest desktop readers
   or replace the generator; allocations remain unchanged. The user approved
   clearing the regenerable uv cache, and dependency installation then finished.
4. `invoice latest` skips nonpayable fortnight outcomes to return the most recent
   payable final since rollout. Cost if wrong: latest can return an older dated
   invoice; explicit dates still privately explain no payable work.
5. Retain the existing engine's consumption of negative corrections in one
   fortnight rather than introduce automatic debt carry-forward. A nonpositive
   outcome needs Sam's reconciliation before later payment. Cost if wrong:
   later invoices may overstate net payment unless Sam records the appropriate
   reviewed adjustment. This limitation is documented in the operating guide.
6. Production provider behavior, deployed scopes/mounts and actual desktop-reader
   interoperability remain rollout checks. Offline proofs do not certify those
   environments. Cost if wrong: deployment stays deferred until those checks
   and deployment review are completed.

## Deferred minor

Non-Latin descriptions and emoji display as explicit Unicode escape sequences
with the standard PDF font. Source records preserve Unicode and work is not
truncated, but an embedded font with broader glyph coverage would improve
readability. The review graded this as minor; it was deferred.
