"""API-level tests: acceptance rules, atomicity, audit trail and catalog queries."""
from __future__ import annotations

import json


def test_health_reports_catalog(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["crs"] == "EPSG:32650"
    assert body["active_images"] == 12
    assert body["total_images"] == 12


def test_list_active_catalog_default(client):
    resp = client.get("/images")
    assert resp.status_code == 200
    sheets = [item["sheet_no"] for item in resp.json()]
    assert sheets == ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
    assert all(item["is_active"] for item in resp.json())


def test_successful_withdrawal_multi_coverer(client):
    # A is fully covered by finer halves B + C.
    resp = client.post("/withdrawals", json={"sheet_nos": ["A"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "committed"
    assert body["withdrawn_sheets"] == ["A"]
    assert body["withdrawn_count"] == 1
    assert body["batch_no"].startswith("WD-")

    a = client.get("/images/A").json()
    assert a["is_active"] is False
    # B and C stay on shelf.
    assert client.get("/images/B").json()["is_active"] is True
    assert client.get("/images/C").json()["is_active"] is True


def test_successful_multi_sheet_batch(client):
    # Both A and K are individually covered by finer survivors.
    resp = client.post("/withdrawals", json={"sheet_nos": ["A", "K"]})
    assert resp.status_code == 200
    assert sorted(resp.json()["withdrawn_sheets"]) == ["A", "K"]


def test_gap_reports_sheet_and_one_uncovered_rectangle(client):
    # E only covers the eastern half of D.
    resp = client.post("/withdrawals", json={"sheet_nos": ["D"]})
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["failed_sheet"] == "D"
    rect = body["uncovered_rect"]
    assert rect == {"min_x": 200, "min_y": 0, "max_x": 250, "max_y": 100}
    # Positive area, inside the target footprint.
    assert (rect["max_x"] - rect["min_x"]) * (
        rect["max_y"] - rect["min_y"]
    ) > 0


def test_coarser_coverer_does_not_count(client):
    # G has the same footprint as F but 1.0 m/px > 0.5 m/px: no valid coverer.
    resp = client.post("/withdrawals", json={"sheet_nos": ["F"]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["failed_sheet"] == "F"
    assert detail["uncovered_rect"] == {
        "min_x": 400,
        "min_y": 0,
        "max_x": 500,
        "max_y": 100,
    }


def test_unknown_sheet_rejected(client):
    resp = client.post("/withdrawals", json={"sheet_nos": ["NOPE"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_sheet"] == "NOPE"
    assert "not found" in resp.json()["detail"]["detail"]


def test_withdrawn_sheet_cannot_be_withdrawn_again(client):
    assert client.post("/withdrawals", json={"sheet_nos": ["A"]}).status_code == 200
    resp = client.post("/withdrawals", json={"sheet_nos": ["A"]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["failed_sheet"] == "A"
    assert "already withdrawn" in detail["detail"]


def test_rejected_batch_changes_nothing_atomic(client):
    before = {
        item["sheet_no"]: item["is_active"]
        for item in client.get("/images", params={"active": False}).json()
    }
    # A would succeed, D must fail: the whole batch is void.
    resp = client.post("/withdrawals", json={"sheet_nos": ["A", "D"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_sheet"] == "D"
    after = {
        item["sheet_no"]: item["is_active"]
        for item in client.get("/images", params={"active": False}).json()
    }
    assert before == after


def test_batch_members_cannot_cover_each_other(client):
    # H and I are duplicate 0.5 m/px sheets; finer J is beneath them.
    # Withdrawing H alone works (I + J survive).
    assert client.post(
        "/withdrawals", json={"sheet_nos": ["H"]}
    ).status_code == 200
    # Now withdrawing I while also trying to withdraw J fails: J is in the
    # same batch and therefore cannot serve as a coverer.
    resp = client.post("/withdrawals", json={"sheet_nos": ["I", "J"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_sheet"] == "I"


def test_duplicate_sheets_in_request_are_deduped(client):
    resp = client.post("/withdrawals", json={"sheet_nos": ["A", "A", "A"]})
    assert resp.status_code == 200
    assert resp.json()["withdrawn_sheets"] == ["A"]


def test_empty_batch_rejected_by_validation(client):
    resp = client.post("/withdrawals", json={"sheet_nos": []})
    assert resp.status_code == 422


def test_audit_trail_records_committed_and_rejected(client):
    client.post("/withdrawals", json={"sheet_nos": ["A"]})
    client.post("/withdrawals", json={"sheet_nos": ["D"]})  # rejected
    client.post("/withdrawals", json={"sheet_nos": ["A"]})  # rejected
    resp = client.get("/withdrawals")
    assert resp.status_code == 200
    batches = resp.json()
    assert [b["status"] for b in batches] == ["rejected", "rejected", "committed"]
    rejected_gap = next(b for b in batches if b["failed_sheet"] == "D")
    assert rejected_gap["reason"] == "coverage gap after withdrawal"
    assert rejected_gap["uncovered_rect"]["min_x"] == 200
    committed = next(b for b in batches if b["status"] == "committed")
    assert committed["requested_sheets"] == ["A"]


def test_inactive_filter_shows_full_catalog(client):
    client.post("/withdrawals", json={"sheet_nos": ["A"]})
    active_only = client.get("/images").json()
    full = client.get("/images", params={"active": False}).json()
    assert len(full) == len(active_only) + 1
    assert any(not item["is_active"] for item in full)


def test_single_image_404(client):
    assert client.get("/images/MISSING").status_code == 404


def test_reinitialize_is_idempotent_and_force_rebuilds(client):
    client.post("/withdrawals", json={"sheet_nos": ["A"]})
    resp = client.post("/admin/reinitialize")
    assert resp.json()["initialized"] is False
    assert client.get("/images/A").json()["is_active"] is False

    forced = client.post("/admin/reinitialize", params={"force": True})
    assert forced.status_code == 200
    assert forced.json()["initialized"] is True
    assert client.get("/images/A").json()["is_active"] is True


def test_database_file_is_the_only_mutable_artifact(client, catalog_paths):
    catalog_file, _ = catalog_paths
    before = catalog_file.read_bytes()
    client.post("/withdrawals", json={"sheet_nos": ["A"]})
    # The JSON supply catalog must remain byte-identical; only the DB changes.
    assert catalog_file.read_bytes() == before
    line = json.loads(catalog_file.read_text())
    assert all(img["file_path"] for img in line["images"])
