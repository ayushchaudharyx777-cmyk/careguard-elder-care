"""Unit and integration tests for CareGuard."""

import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event

# Set environment variables before importing app components
os.environ["DATABASE_URL"] = "sqlite:///./test_careguard.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["CRON_SECRET"] = "test-cron-secret"
os.environ["SMTP_HOST"] = ""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.notifications as notifications
from app.core import (
    Appointment,
    Base,
    DoseLog,
    Medication,
    Patient,
    SessionLocal,
    engine,
    utcnow,
)
from app.main import app


@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def captured_emails(monkeypatch):
    emails: list[dict[str, str]] = []

    def mock_send(to_email: str, subject: str, body: str) -> bool:
        emails.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(notifications, "send_email", mock_send)
    return emails


def test_setup_runs_only_once(client):
    """1. setup runs only once (second call 409)."""
    payload = {
        "patient_name": "Eleanor Vance",
        "patient_timezone": "Asia/Kolkata",
        "caregiver_name": "Sarah Vance",
        "caregiver_email": "sarah@example.com",
        "caregiver_passcode": "securepass123",
    }
    res1 = client.post("/api/setup", json=payload)
    assert res1.status_code == 201
    data1 = res1.json()
    assert "token" in data1
    assert data1["member"]["email"] == "sarah@example.com"
    assert data1["member"]["role"] == "caregiver"

    res2 = client.post("/api/setup", json=payload)
    assert res2.status_code == 409


def test_protected_routes_require_valid_session(client):
    """2. protected routes need a valid session (401 without token and with a bad token)."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    valid_token = setup_res.json()["token"]

    # Without token
    res_no_token = client.get("/api/me")
    assert res_no_token.status_code == 401

    # With bad token
    res_bad_token = client.get("/api/me", headers={"Authorization": "Bearer bad-invalid-token"})
    assert res_bad_token.status_code == 401

    # With valid token
    res_valid = client.get("/api/me", headers={"Authorization": f"Bearer {valid_token}"})
    assert res_valid.status_code == 200
    assert res_valid.json()["member"]["email"] == "sarah@example.com"


def test_five_wrong_passcodes_lock_account(client):
    """3. five wrong passcodes lock the account (sixth correct attempt returns 429)."""
    client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "correctpass123",
        },
    )

    # Five wrong attempts
    for _ in range(5):
        res = client.post(
            "/api/auth/login",
            json={"email": "sarah@example.com", "passcode": "wrongpass"},
        )
        assert res.status_code == 401

    # Sixth attempt with correct passcode
    res6 = client.post(
        "/api/auth/login",
        json={"email": "sarah@example.com", "passcode": "correctpass123"},
    )
    assert res6.status_code == 429


def test_times_like_8_00_rejected(client):
    """4. times like '8:00' are rejected (422)."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "name": "Metformin",
        "dosage": "500mg",
        "instructions": "Take with breakfast",
        "times": ["8:00"],  # Invalid format; must be "08:00"
        "start_date": "2026-11-15",
    }
    res = client.post("/api/medications", json=payload, headers=headers)
    assert res.status_code == 422


def test_upcoming_dose_lifecycle(client, captured_emails):
    """5.

    upcoming dose: first cycle at 2026-11-15 02:30 UTC sends one reminder per
    member; second cycle sends none; PATCH to taken works; a later cycle at 03:45
    UTC does not mark it missed.
    """
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Add family member so care circle has multiple members
    client.post(
        "/api/members",
        json={
            "name": "John Vance",
            "email": "john@example.com",
            "passcode": "johnpass123",
            "role": "family",
        },
        headers=headers,
    )

    # Create medication and set created_at directly in database to 2026-11-15 00:00 UTC
    db: Session = SessionLocal()
    patient = db.query(Patient).first()
    med = Medication(
        patient_id=patient.id,
        name="Blood Pressure Med",
        dosage="10mg",
        instructions="Take morning",
        times=["08:00"],
        start_date=date(2026, 11, 15),
        active=True,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )
    db.add(med)
    db.commit()
    db.refresh(med)
    db.close()

    # Cycle 1 at 2026-11-15 02:30 UTC
    # 08:00 Asia/Kolkata is 02:30 UTC
    now1 = datetime(2026, 11, 15, 2, 30, 0)
    stats1 = notifications.run_reminder_cycle(now=now1)
    assert stats1["upcoming"] == 1
    assert stats1["missed"] == 0

    # Should send one reminder per member (2 members: Sarah & John)
    assert len(captured_emails) == 2
    assert all("Medicine reminder" in e["subject"] for e in captured_emails)
    captured_emails.clear()

    # Cycle 2 at same time (or slightly later) sends none
    stats2 = notifications.run_reminder_cycle(now=now1)
    assert stats2["upcoming"] == 0
    assert stats2["missed"] == 0
    assert len(captured_emails) == 0

    # PATCH to taken works
    doses_res = client.get("/api/doses", headers=headers)
    doses = doses_res.json()
    assert len(doses) == 1
    dose_id = doses[0]["id"]
    assert doses[0]["status"] == "pending"

    patch_res = client.patch(
        f"/api/doses/{dose_id}",
        json={"status": "taken"},
        headers=headers,
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["status"] == "taken"

    # A later cycle at 03:45 UTC does not mark it missed
    now3 = datetime(2026, 11, 15, 3, 45, 0)
    stats3 = notifications.run_reminder_cycle(now=now3)
    assert stats3["missed"] == 0
    assert len(captured_emails) == 0

    db = SessionLocal()
    dose_db = db.query(DoseLog).filter(DoseLog.id == dose_id).first()
    assert dose_db.status == "taken"
    db.close()


def test_missed_dose_and_appointments(client, captured_emails):
    """6.

    missed dose: at 03:45 UTC the pending dose becomes missed and a "Missed dose"
    email is sent; an appointment 5 hours away gets one reminder and a far one
    gets none.
    """
    client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )

    db: Session = SessionLocal()
    patient = db.query(Patient).first()

    med = Medication(
        patient_id=patient.id,
        name="Heart Med",
        dosage="25mg",
        instructions="With breakfast",
        times=["08:00"],
        start_date=date(2026, 11, 15),
        active=True,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )
    db.add(med)
    db.flush()

    # Create pending dose for 02:30 UTC
    slot = datetime(2026, 11, 15, 2, 30, 0)
    pending_dose = DoseLog(
        medication_id=med.id,
        scheduled_for=slot,
        status="pending",
        created_at=datetime(2026, 11, 15, 2, 20, 0),
        updated_at=datetime(2026, 11, 15, 2, 20, 0),
    )
    db.add(pending_dose)

    # Appointment 5 hours away from 03:45 UTC -> 08:45 UTC
    now_test = datetime(2026, 11, 15, 3, 45, 0)
    near_appt = Appointment(
        patient_id=patient.id,
        doctor_name="Dr. Near",
        specialty="Cardiology",
        location="Room 101",
        starts_at=now_test + timedelta(hours=5),
        reminder_sent=False,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )
    # Appointment far away (48 hours away)
    far_appt = Appointment(
        patient_id=patient.id,
        doctor_name="Dr. Far",
        specialty="Dermatology",
        location="Room 202",
        starts_at=now_test + timedelta(hours=48),
        reminder_sent=False,
        created_at=datetime(2026, 11, 15, 0, 0, 0),
    )
    db.add(near_appt)
    db.add(far_appt)
    db.commit()
    db.close()

    # Run reminder cycle at 03:45 UTC
    stats = notifications.run_reminder_cycle(now=now_test)
    assert stats["missed"] == 1
    assert stats["appointments"] == 1

    # Check emails
    missed_emails = [e for e in captured_emails if "Missed dose" in e["subject"]]
    assert len(missed_emails) >= 1

    appt_emails = [e for e in captured_emails if "Appointment" in e["subject"]]
    assert len(appt_emails) >= 1
    assert any("Dr. Near" in e["body"] for e in appt_emails)
    assert not any("Dr. Far" in e["body"] for e in appt_emails)

    # Verify dose became missed in DB
    db = SessionLocal()
    dose_db = db.query(DoseLog).filter(DoseLog.scheduled_for == slot).first()
    assert dose_db.status == "missed"
    db.close()


def test_cron_endpoint_requires_secret(client):
    """7. cron endpoint returns 403 without the secret and 200 with it."""
    # Without secret header
    res_no_secret = client.post("/api/reminders/run")
    assert res_no_secret.status_code == 403

    # With wrong secret
    res_bad_secret = client.post("/api/reminders/run", headers={"X-Cron-Secret": "invalid-secret"})
    assert res_bad_secret.status_code == 403

    # With correct secret
    res_good_secret = client.post(
        "/api/reminders/run", headers={"X-Cron-Secret": "test-cron-secret"}
    )
    assert res_good_secret.status_code == 200
    assert res_good_secret.json()["status"] == "ok"


def test_family_member_cannot_add_member(client):
    """8. a family member cannot add a member (403)."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "cgpass123",
        },
    )
    cg_token = setup_res.json()["token"]

    # Caregiver adds family member
    add_res = client.post(
        "/api/members",
        json={
            "name": "John Vance",
            "email": "john@example.com",
            "passcode": "johnpass123",
            "role": "family",
        },
        headers={"Authorization": f"Bearer {cg_token}"},
    )
    assert add_res.status_code == 201

    # Family member signs in
    login_res = client.post(
        "/api/auth/login",
        json={"email": "john@example.com", "passcode": "johnpass123"},
    )
    assert login_res.status_code == 200
    family_token = login_res.json()["token"]

    # Family member attempts to add another member
    res = client.post(
        "/api/members",
        json={
            "name": "Alice Vance",
            "email": "alice@example.com",
            "passcode": "alicepass123",
            "role": "family",
        },
        headers={"Authorization": f"Bearer {family_token}"},
    )
    assert res.status_code == 403


def test_security_headers_present_on_get_root(client):
    """9. security headers are present on GET /."""
    res = client.get("/")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert res.headers.get("Referrer-Policy") == "no-referrer"
    assert (
        res.headers.get("Content-Security-Policy")
        == "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src"
        " 'self'; frame-ancestors 'none'"
    )


def test_gzip_compression(client):
    """Verify responses greater than threshold are compressed with gzip."""
    res = client.get("/static/styles.css", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers.get("Content-Encoding") == "gzip"


def test_static_and_root_cache_headers(client):
    """Verify static assets are cached while html is no-cache."""
    res_static = client.get("/static/styles.css")
    assert res_static.status_code == 200
    assert "public, max-age=3600" in res_static.headers.get("Cache-Control", "")

    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "no-cache" in res_root.headers.get("Cache-Control", "")


def test_pagination_defaults_and_limits(client):
    """Verify pagination defaults return full list while limit/offset slice correctly."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(4):
        client.post(
            "/api/medications",
            json={
                "name": f"Med-{i}",
                "dosage": "10mg",
                "instructions": "Take daily",
                "times": ["08:00"],
                "start_date": "2026-11-15",
            },
            headers=headers,
        )

    # Default without pagination returns all 4
    res_all = client.get("/api/medications", headers=headers)
    assert res_all.status_code == 200
    assert len(res_all.json()) == 4

    # Limit = 2
    res_limit = client.get("/api/medications?limit=2", headers=headers)
    assert res_limit.status_code == 200
    assert len(res_limit.json()) == 2
    assert res_limit.json()[0]["name"] == "Med-0"

    # Limit = 2 with Offset = 2
    res_offset = client.get("/api/medications?limit=2&offset=2", headers=headers)
    assert res_offset.status_code == 200
    assert len(res_offset.json()) == 2
    assert res_offset.json()[0]["name"] == "Med-2"


def test_reminder_cycle_idempotency(client):
    """Verify running the reminder cycle multiple times creates no duplicates."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Add medication
    client.post(
        "/api/medications",
        json={
            "name": "Daily Vitamin",
            "dosage": "1 tab",
            "times": ["08:00"],
            "start_date": "2026-11-15",
        },
        headers=headers,
    )

    db = SessionLocal()
    med = db.query(Medication).first()
    med.created_at = datetime(2026, 11, 15, 0, 0, 0)
    db.commit()
    db.close()

    now = datetime(2026, 11, 15, 2, 30, 0)
    stats1 = notifications.run_reminder_cycle(now=now)
    assert stats1["upcoming"] == 1

    # Second run at same time must be idempotent
    stats2 = notifications.run_reminder_cycle(now=now)
    assert stats2["upcoming"] == 0
    assert stats2["missed"] == 0
    assert stats2["appointments"] == 0

    db = SessionLocal()
    dose_count = db.query(DoseLog).count()
    assert dose_count == 1
    db.close()


def test_query_count_no_n_plus_one(client):
    """Verify list endpoints execute a constant number of queries without N+1 regression."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(5):
        client.post(
            "/api/medications",
            json={
                "name": f"Medication {i}",
                "dosage": "20mg",
                "instructions": "Daily",
                "times": ["09:00"],
                "start_date": "2026-11-15",
            },
            headers=headers,
        )

    queries = []

    def count_query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        queries.clear()
        res = client.get("/api/medications", headers=headers)
        assert res.status_code == 200
        # 1 query for auth (session + member + patient) and 1 query for medications
        assert len(queries) <= 2

        queries.clear()
        res_doses = client.get("/api/doses", headers=headers)
        assert res_doses.status_code == 200
        # 1 query for auth and 1 query for doses with joinedload
        assert len(queries) <= 2
    finally:
        event.remove(engine, "before_cursor_execute", count_query)


def test_caregiver_cannot_remove_self(client):
    """Verify caregiver cannot delete their own account."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    data = setup_res.json()
    token = data["token"]
    cg_id = data["member"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.delete(f"/api/members/{cg_id}", headers=headers)
    assert res.status_code == 400
    assert "cannot remove themselves" in res.json()["detail"]


def test_member_error_paths(client):
    """Verify member deletion 404, duplicate email 409, and family deletion 403."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    cg_token = setup_res.json()["token"]
    cg_headers = {"Authorization": f"Bearer {cg_token}"}

    # Delete non-existent member -> 404
    del_404 = client.delete("/api/members/99999", headers=cg_headers)
    assert del_404.status_code == 404

    # Add duplicate email -> 409
    dup = client.post(
        "/api/members",
        json={
            "name": "Sarah Clone",
            "email": "sarah@example.com",
            "passcode": "dup12345",
            "role": "caregiver",
        },
        headers=cg_headers,
    )
    assert dup.status_code == 409

    # Add valid family member
    add_fam = client.post(
        "/api/members",
        json={
            "name": "Tom Vance",
            "email": "tom@example.com",
            "passcode": "tompass123",
            "role": "family",
        },
        headers=cg_headers,
    )
    assert add_fam.status_code == 201
    tom_id = add_fam.json()["id"]

    # Family member logs in
    login_fam = client.post(
        "/api/auth/login",
        json={"email": "tom@example.com", "passcode": "tompass123"},
    )
    fam_token = login_fam.json()["token"]
    fam_headers = {"Authorization": f"Bearer {fam_token}"}

    # Family member tries to delete member -> 403
    del_403 = client.delete(f"/api/members/{tom_id}", headers=fam_headers)
    assert del_403.status_code == 403

    # Caregiver removes family member -> 200
    del_200 = client.delete(f"/api/members/{tom_id}", headers=cg_headers)
    assert del_200.status_code == 200


def test_medication_put_and_delete_lifecycle(client):
    """Verify updating and deleting a medication."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    headers = {"Authorization": f"Bearer {setup_res.json()['token']}"}

    # Add med
    med_res = client.post(
        "/api/medications",
        json={
            "name": "Metformin",
            "dosage": "500mg",
            "instructions": "With breakfast",
            "times": ["08:00"],
            "start_date": "2026-11-15",
        },
        headers=headers,
    )
    med_id = med_res.json()["id"]

    # PUT update
    update_res = client.put(
        f"/api/medications/{med_id}",
        json={
            "name": "Metformin Extended Release",
            "dosage": "1000mg",
            "instructions": "With dinner",
            "times": ["20:00"],
            "start_date": "2026-11-15",
            "active": True,
        },
        headers=headers,
    )
    assert update_res.status_code == 200
    assert update_res.json()["name"] == "Metformin Extended Release"

    # PUT non-existent med -> 404
    put_404 = client.put(
        "/api/medications/99999",
        json={
            "name": "Ghost",
            "dosage": "10mg",
            "times": ["08:00"],
            "start_date": "2026-11-15",
        },
        headers=headers,
    )
    assert put_404.status_code == 404

    # DELETE med
    del_res = client.delete(f"/api/medications/{med_id}", headers=headers)
    assert del_res.status_code == 200

    # DELETE non-existent med -> 404
    del_404 = client.delete("/api/medications/99999", headers=headers)
    assert del_404.status_code == 404


def test_appointment_lifecycle_and_not_found(client):
    """Verify appointment creation, retrieval, 404 on foreign ID, and deletion."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    headers = {"Authorization": f"Bearer {setup_res.json()['token']}"}

    create_res = client.post(
        "/api/appointments",
        json={
            "doctor_name": "Dr. Sarah Adams",
            "specialty": "Neurology",
            "location": "Suite 400",
            "notes": "Annual check",
            "starts_at": "2026-11-20T10:00:00",
        },
        headers=headers,
    )
    assert create_res.status_code == 201
    appt_id = create_res.json()["id"]

    # List appointments
    list_res = client.get("/api/appointments", headers=headers)
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # Delete foreign / non-existent -> 404
    assert client.delete("/api/appointments/99999", headers=headers).status_code == 404

    # Delete appointment -> 200
    assert client.delete(f"/api/appointments/{appt_id}", headers=headers).status_code == 200


def test_dose_patch_and_adherence(client):
    """Verify dose patch validation, not found, and adherence calculation."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    headers = {"Authorization": f"Bearer {setup_res.json()['token']}"}

    # Dose not found
    d_404 = client.patch("/api/doses/99999", json={"status": "taken"}, headers=headers)
    assert d_404.status_code == 404

    # Invalid status
    d_422 = client.patch("/api/doses/99999", json={"status": "invalid"}, headers=headers)
    assert d_422.status_code == 422

    # Check adherence when empty
    adh_empty = client.get("/api/adherence?days=7", headers=headers)
    assert adh_empty.status_code == 200
    assert adh_empty.json()["adherence"] is None

    # Insert test doses directly
    db = SessionLocal()
    patient = db.query(Patient).first()
    med = Medication(
        patient_id=patient.id,
        name="Statin",
        dosage="20mg",
        instructions="",
        times=["08:00"],
        start_date=date(2026, 11, 1),
        active=True,
    )
    db.add(med)
    db.flush()

    now = utcnow()
    d1 = DoseLog(medication_id=med.id, scheduled_for=now - timedelta(days=2), status="taken")
    d2 = DoseLog(medication_id=med.id, scheduled_for=now - timedelta(days=1), status="skipped")
    d3 = DoseLog(medication_id=med.id, scheduled_for=now - timedelta(hours=5), status="missed")
    db.add_all([d1, d2, d3])
    db.commit()
    db.close()

    adh_res = client.get("/api/adherence?days=7", headers=headers)
    assert adh_res.status_code == 200
    data = adh_res.json()
    assert data["taken"] == 1
    assert data["skipped"] == 1
    assert data["missed"] == 1
    assert data["total_due"] == 3
    assert data["adherence"] == 33.3


def test_auth_logout_and_invalid_credentials(client):
    """Verify logout invalidates the token and invalid logins return 401."""
    setup_res = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Asia/Kolkata",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    token = setup_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Logout
    logout_res = client.post("/api/auth/logout", headers=headers)
    assert logout_res.status_code == 200

    # Subsequent request using same token must fail with 401
    assert client.get("/api/me", headers=headers).status_code == 401

    # Login with non-existent email
    bad_login = client.post(
        "/api/auth/login",
        json={"email": "nonexistent@example.com", "passcode": "anypass"},
    )
    assert bad_login.status_code == 401


def test_invalid_timezone_rejected(client):
    """Verify invalid patient timezone is rejected during setup."""
    bad_tz = client.post(
        "/api/setup",
        json={
            "patient_name": "Eleanor Vance",
            "patient_timezone": "Moon/Base_Alpha",
            "caregiver_name": "Sarah Vance",
            "caregiver_email": "sarah@example.com",
            "caregiver_passcode": "securepass123",
        },
    )
    assert bad_tz.status_code == 422


def test_send_email_smtp_live_mock(monkeypatch):
    """Verify send_email with configured SMTP host and credentials."""
    from app.core import settings

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test.local")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "user@test.local")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SMTP_STARTTLS", True)

    mock_server = MagicMock()
    mock_smtp_class = MagicMock(return_value=mock_server)
    mock_server.__enter__.return_value = mock_server

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", mock_smtp_class)

    ok = notifications.send_email("patient@test.local", "Test Subject", "Test Body")
    assert ok is True
    assert mock_server.starttls.called
    assert mock_server.login.called
    assert mock_server.send_message.called


def test_empty_database_reminder_cycle():
    """Verify reminder cycle with empty database runs safely and returns zeros."""
    res = notifications.run_reminder_cycle()
    assert res == {"upcoming": 0, "missed": 0, "appointments": 0}


def test_notifications_helper_functions_direct():
    """Direct unit testing of _process_slot and _process_appointments helpers."""
    db = SessionLocal()
    patient = Patient(name="Test Patient", timezone="UTC")
    db.add(patient)
    db.flush()

    med = Medication(
        patient_id=patient.id,
        name="Med A",
        dosage="5mg",
        instructions="",
        times=["10:00"],
        start_date=date(2026, 11, 1),
        active=True,
    )
    db.add(med)
    db.flush()

    slot = datetime(2026, 11, 15, 10, 0, 0)
    # Slot in future (> 15 min ahead) -> (0, 0)
    now_early = datetime(2026, 11, 15, 9, 0, 0)
    up1, mi1 = notifications._process_slot(db, med, slot, now_early, patient)
    assert (up1, mi1) == (0, 0)

    # Slot past 60 min -> missed
    now_late = datetime(2026, 11, 15, 11, 15, 0)
    up2, mi2 = notifications._process_slot(db, med, slot, now_late, patient)
    assert (up2, mi2) == (0, 1)

    # _process_appointments with appointment in window
    appt = Appointment(
        patient_id=patient.id,
        doctor_name="Dr. Helper",
        starts_at=now_late + timedelta(hours=2),
        reminder_sent=False,
    )
    db.add(appt)
    db.commit()

    cnt = notifications._process_appointments(db, patient, now_late)
    assert cnt == 1
    db.close()


def test_send_email_failure_handling(monkeypatch):
    """Verify send_email gracefully returns False on SMTP exception."""
    from app.core import settings

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid.domain")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)

    import smtplib

    def mock_smtp_err(*args, **kwargs):
        raise smtplib.SMTPConnectError(550, b"Connection failed")

    monkeypatch.setattr(smtplib, "SMTP", mock_smtp_err)
    ok = notifications.send_email("patient@test.local", "Subject", "Body")
    assert ok is False


def test_overlapping_scheduler_lock(monkeypatch):
    """Verify overlapping reminder cycle execution is skipped."""
    import threading

    fake_lock = threading.Lock()
    fake_lock.acquire()
    monkeypatch.setattr(notifications, "_cycle_lock", fake_lock)

    res = notifications.run_reminder_cycle()
    assert res == {"upcoming": 0, "missed": 0, "appointments": 0}
