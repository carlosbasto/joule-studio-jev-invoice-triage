---
name: invoice-intake
description: Validates a blocked-invoice triage request before analysis — ensures an invoice number and fiscal year are present, identifies the block reason, and lists the evidence tools to gather.
---

# Invoice intake

Use this at the start of every blocked-invoice request to make sure you have what you need before proposing a payment-block release.

## Required before proceeding
- **Invoice number** — e.g. `INV-5500011`. If missing, ask the AP user for it (one question).
- **Fiscal year** — e.g. `2026`. Needed to identify the document uniquely. If missing and it cannot be inferred, ask.
- **Block reason** — read from the invoice detail. Common reasons:
  - `price_variance` — invoiced price is outside PO tolerance.
  - `quantity_variance` — invoiced quantity exceeds the ordered/received quantity.
  - `goods_receipt_missing` — no goods receipt posted for the invoiced quantity.
  - `duplicate_suspected` — a similar invoice already exists for the vendor.
  - `manual_block` — a clerk or approver set a manual payment block.

If the invoice number or fiscal year is missing or ambiguous, ask exactly one targeted clarifying question and stop.

## Evidence to gather (call these tools)
1. `get_invoice_detail(invoice_number, fiscal_year)` — header, line items, gross amount, vendor, block reason, PO/goods-receipt match status.
2. `get_recent_invoices_for_vendor(vendor_id, lookback_days)` — detect a duplicate (same reference or same amount close in time).
3. `get_blocked_invoices(company_code, date_from, date_to)` — only when triaging a batch / finding the right invoice.

## Turn evidence into a grounded case summary
Write a single, checkable summary (e.g. "Invoice INV-5500011 is blocked for a 3% price variance; PO 4500001234 line 10 matches within the 5% tolerance and a goods receipt is posted for the full quantity"). Keep it specific and grounded in tool output. This summary supports the agent's reasoning and user-facing explanation; the live release guard independently re-reads the invoice and recent-vendor evidence before it asks JEV whether `release_payment_block` may execute.
