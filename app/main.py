"""FastAPI application for auditable, human-reviewed invoice intake."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .extraction import extract_invoice, validate_invoice


class Intake(BaseModel):
    source_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=10, max_length=100_000)


class Decision(BaseModel):
    reviewer: str = Field(min_length=2, max_length=100)
    reason: str = Field(default="", max_length=500)


class Correction(BaseModel):
    reviewer: str = Field(min_length=2, max_length=100)
    invoice_number: str | None = None
    vendor: str | None = None
    invoice_date: str | None = None
    subtotal: str | None = None
    tax: str | None = None
    total: str | None = None
    currency: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path or os.environ.get("INVOICE_DB", "invoice_review.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Invoice Review API", version="1.0.0")

    @contextmanager
    def connect():
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, raw_text TEXT NOT NULL,
            extracted TEXT NOT NULL, issues TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT NOT NULL,
            action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL, FOREIGN KEY(invoice_id) REFERENCES invoices(id)
        )""")

    def serialize(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["extracted"] = json.loads(result["extracted"])
        result["issues"] = json.loads(result["issues"])
        return result

    def duplicate_exists(db: sqlite3.Connection, extracted: dict, exclude_id: str | None = None) -> bool:
        if not extracted["invoice_number"] or not extracted["vendor"]:
            return False
        rows = db.execute("SELECT id, extracted FROM invoices WHERE id != ?", (exclude_id or "",)).fetchall()
        for row in rows:
            previous = json.loads(row["extracted"])
            if (previous["invoice_number"] == extracted["invoice_number"]
                    and previous["vendor"] and previous["vendor"].casefold() == extracted["vendor"].casefold()):
                return True
        return False

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/invoices", status_code=201)
    def intake(payload: Intake):
        extracted = extract_invoice(payload.text)
        issues = validate_invoice(extracted)
        timestamp = now()
        invoice_id = str(uuid4())
        with connect() as db:
            existing = db.execute("SELECT * FROM invoices WHERE source_id = ?", (payload.source_id,)).fetchone()
            if existing:
                if existing["raw_text"] == payload.text:
                    return serialize(existing) | {"idempotent_replay": True}
                raise HTTPException(409, "source_id already exists with different content")
            if duplicate_exists(db, extracted):
                issues.append("possible_duplicate_invoice")
            db.execute("INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                invoice_id, payload.source_id, payload.text, json.dumps(extracted),
                json.dumps(issues), "needs_review", timestamp, timestamp,
            ))
            db.execute("INSERT INTO events (invoice_id, action, actor, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                       (invoice_id, "ingested", "system", "", timestamp))
            row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return serialize(row) | {"idempotent_replay": False}

    @app.get("/api/invoices")
    def list_invoices(status: str | None = Query(default=None, pattern="^(needs_review|approved|rejected)$")):
        with connect() as db:
            rows = db.execute("SELECT * FROM invoices WHERE (? IS NULL OR status = ?) ORDER BY created_at DESC",
                              (status, status)).fetchall()
        return [serialize(row) for row in rows]

    @app.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: str):
        with connect() as db:
            row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "invoice not found")
            events = db.execute("SELECT action, actor, reason, created_at FROM events WHERE invoice_id = ? ORDER BY id",
                                (invoice_id,)).fetchall()
        return serialize(row) | {"events": [dict(event) for event in events]}

    @app.post("/api/invoices/{invoice_id}/decisions")
    def decide(invoice_id: str, action: str, decision: Decision):
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action must be approve or reject")
        with connect() as db:
            row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "invoice not found")
            if row["status"] != "needs_review":
                raise HTTPException(409, "invoice has already been reviewed")
            if action == "approve" and json.loads(row["issues"]):
                raise HTTPException(422, "resolve validation issues before approval")
            if action == "reject" and not decision.reason.strip():
                raise HTTPException(422, "rejection requires a reason")
            status = "approved" if action == "approve" else "rejected"
            timestamp = now()
            db.execute("UPDATE invoices SET status = ?, updated_at = ? WHERE id = ?",
                       (status, timestamp, invoice_id))
            db.execute("INSERT INTO events (invoice_id, action, actor, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                       (invoice_id, action, decision.reviewer, decision.reason.strip(), timestamp))
            result = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return serialize(result)

    @app.patch("/api/invoices/{invoice_id}")
    def correct(invoice_id: str, correction: Correction):
        changes = correction.model_dump(exclude_unset=True, exclude={"reviewer"})
        if not changes:
            raise HTTPException(422, "provide at least one field to correct")
        for field in ("subtotal", "tax", "total"):
            if field in changes and changes[field] is not None:
                try:
                    amount = Decimal(changes[field])
                    if not amount.is_finite() or amount < 0:
                        raise InvalidOperation
                    changes[field] = str(amount)
                except (InvalidOperation, ValueError):
                    raise HTTPException(422, f"{field} must be a nonnegative number") from None
        if "invoice_date" in changes and changes["invoice_date"] is not None:
            try:
                changes["invoice_date"] = date.fromisoformat(changes["invoice_date"]).isoformat()
            except ValueError:
                raise HTTPException(422, "invoice_date must use YYYY-MM-DD") from None
        for field in ("invoice_number", "vendor", "currency"):
            if field in changes and changes[field] is not None:
                changes[field] = changes[field].strip()
                if not changes[field] or len(changes[field]) > 120:
                    raise HTTPException(422, f"{field} must contain 1 to 120 characters")
        with connect() as db:
            row = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "invoice not found")
            if row["status"] != "needs_review":
                raise HTTPException(409, "reviewed invoice cannot be changed")
            extracted = json.loads(row["extracted"])
            extracted.update(changes)
            issues = validate_invoice(extracted)
            if duplicate_exists(db, extracted, invoice_id):
                issues.append("possible_duplicate_invoice")
            timestamp = now()
            db.execute("UPDATE invoices SET extracted = ?, issues = ?, updated_at = ? WHERE id = ?",
                       (json.dumps(extracted), json.dumps(issues), timestamp, invoice_id))
            db.execute("INSERT INTO events (invoice_id, action, actor, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                       (invoice_id, "corrected", correction.reviewer, ", ".join(sorted(changes)), timestamp))
            result = db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return serialize(result)

    @app.get("/api/metrics")
    def metrics():
        with connect() as db:
            counts = {row["status"]: row["n"] for row in db.execute(
                "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status")}
        return {key: counts.get(key, 0) for key in ("needs_review", "approved", "rejected")}

    return app


app = create_app()
