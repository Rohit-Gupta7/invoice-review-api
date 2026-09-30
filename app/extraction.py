"""Extract an auditable draft from invoice text; never silently approve it."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

AMOUNT = r"(?:INR|Rs\.?|₹|USD|\$)?\s*([\d,]+(?:\.\d{1,2})?)"


def _field(text: str, names: tuple[str, ...]) -> str | None:
    labels = "|".join(re.escape(name) for name in names)
    match = re.search(rf"(?im)^\s*(?:{labels})\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def _money(text: str, names: tuple[str, ...]) -> Decimal | None:
    raw = _field(text, names)
    if raw is None:
        return None
    match = re.fullmatch(AMOUNT, raw.strip(), re.IGNORECASE)
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def extract_invoice(text: str) -> dict:
    """Parse common key/value OCR output; unknown fields stay unknown."""
    invoice_number = _field(text, ("invoice number", "invoice no", "invoice #"))
    vendor = _field(text, ("vendor", "supplier", "seller"))
    raw_date = _field(text, ("invoice date", "date"))
    try:
        invoice_date = date.fromisoformat(raw_date).isoformat() if raw_date else None
    except ValueError:
        invoice_date = None
    subtotal = _money(text, ("subtotal", "sub total", "taxable amount"))
    tax = _money(text, ("tax", "gst", "vat"))
    total = _money(text, ("total", "grand total", "amount due"))
    currency = "USD" if re.search(r"(?i)\bUSD\b|\$", text) else "INR"
    return {
        "invoice_number": invoice_number,
        "vendor": vendor,
        "invoice_date": invoice_date,
        "subtotal": str(subtotal) if subtotal is not None else None,
        "tax": str(tax) if tax is not None else None,
        "total": str(total) if total is not None else None,
        "currency": currency,
    }


def validate_invoice(data: dict) -> list[str]:
    issues: list[str] = []
    for field in ("invoice_number", "vendor", "invoice_date", "subtotal", "tax", "total"):
        if data[field] is None:
            issues.append(f"missing_or_invalid_{field}")
    if all(data[key] is not None for key in ("subtotal", "tax", "total")):
        if abs(Decimal(data["subtotal"]) + Decimal(data["tax"]) - Decimal(data["total"])) > Decimal("0.01"):
            issues.append("amount_mismatch")
    return issues
