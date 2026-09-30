from fastapi.testclient import TestClient

from app.main import create_app


GOOD = """Vendor: Acme Office Supplies
Invoice Number: ACME-2026-1042
Invoice Date: 2026-09-15
Subtotal: INR 1,250.00
Tax: INR 225.00
Total: INR 1,475.00"""


def client(tmp_path):
    return TestClient(create_app(tmp_path / "test.db"))


def test_intake_is_idempotent_and_review_is_audited(tmp_path):
    api = client(tmp_path)
    first = api.post("/api/invoices", json={"source_id": "upload-1", "text": GOOD})
    assert first.status_code == 201
    invoice = first.json()
    assert invoice["issues"] == []
    replay = api.post("/api/invoices", json={"source_id": "upload-1", "text": GOOD})
    assert replay.json()["id"] == invoice["id"]
    assert replay.json()["idempotent_replay"] is True
    assert api.post("/api/invoices", json={"source_id": "upload-1", "text": GOOD + " changed"}).status_code == 409
    approved = api.post(f"/api/invoices/{invoice['id']}/decisions?action=approve",
                        json={"reviewer": "Rohit"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    detail = api.get(f"/api/invoices/{invoice['id']}").json()
    assert [event["action"] for event in detail["events"]] == ["ingested", "approve"]
    assert api.get("/api/metrics").json()["approved"] == 1


def test_validation_correction_and_duplicate_detection(tmp_path):
    api = client(tmp_path)
    bad = GOOD.replace("1,475.00", "1,400.00")
    first = api.post("/api/invoices", json={"source_id": "a", "text": bad}).json()
    assert "amount_mismatch" in first["issues"]
    assert api.post(f"/api/invoices/{first['id']}/decisions?action=approve",
                    json={"reviewer": "Rohit"}).status_code == 422
    assert api.patch(f"/api/invoices/{first['id']}",
                     json={"reviewer": "Rohit", "total": "1475.00"}).json()["issues"] == []
    assert api.post(f"/api/invoices/{first['id']}/decisions?action=approve",
                    json={"reviewer": "Rohit"}).status_code == 200
    second = api.post("/api/invoices", json={"source_id": "b", "text": GOOD}).json()
    assert "possible_duplicate_invoice" in second["issues"]
    assert api.post(f"/api/invoices/{second['id']}/decisions?action=reject",
                    json={"reviewer": "Rohit", "reason": "Duplicate"}).status_code == 200


def test_invalid_inputs_and_missing_invoice(tmp_path):
    api = client(tmp_path)
    assert api.post("/api/invoices", json={"source_id": "a", "text": "short"}).status_code == 422
    assert api.get("/api/invoices/unknown").status_code == 404
    invoice = api.post("/api/invoices", json={"source_id": "a", "text": GOOD}).json()
    assert api.patch(f"/api/invoices/{invoice['id']}",
                     json={"reviewer": "Rohit", "tax": "NaN"}).status_code == 422
