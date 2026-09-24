---
name: release-policy
description: How payment-block release decisions are made and gated — what evidence justifies release for each block reason, and how the JEV release guard controls the state-changing release before the original tool can execute.
---

# Release policy & decision gates

Payment-block releases are **not** decided by free-text judgment alone. Your job is to gather grounded evidence and propose the appropriate business action. If you call `release_payment_block`, the live Python wrapper re-reads the relevant evidence and asks JEV (TypeSafe System One) for a typed release verdict. Only an explicit `allow` invokes the original release tool; other or unavailable decisions hold or deny the release.

## What justifies release for each block reason
- `price_variance` — release only when the invoiced price is within PO tolerance (per `get_invoice_detail` match status). Outside tolerance: do not release; route to a clerk or escalate.
- `quantity_variance` — release only when the invoiced quantity matches the ordered/received quantity within tolerance.
- `goods_receipt_missing` — do NOT release unless a goods receipt is posted for the invoiced quantity. No GR on file → keep the block.
- `duplicate_suspected` — check `get_recent_invoices_for_vendor`. If the reference or amount matches a recent invoice, treat as a likely duplicate: do not release; escalate.
- `manual_block` — release only with a clear, documented reason the block can be lifted; otherwise route to the clerk who set it.

## The live JEV release gate
The running agent installs one JEV gate directly around `release_payment_block`. The gate returns one of four typed choices: `allow`, `confirm`, `review`, or `deny`. Only `allow` invokes the original release coroutine. `confirm` / `review` return a pending-review status, `deny` returns a denial, and a disabled, failed, missing, or invalid JEV decision also holds the release.


## Your responsibilities
- Call `release_payment_block` only after you have gathered and cited evidence.
- If `release_payment_block` returns `release_denied` or `release_pending_review`, relay that to the AP user verbatim; do not retry or claim success.
- Only state a block was released when the response contains a `release_id`.
- When release is not justified, call `create_clerk_task` (needs a human to reconcile) or `escalate_to_manager` (duplicate / fraud / high severity).
