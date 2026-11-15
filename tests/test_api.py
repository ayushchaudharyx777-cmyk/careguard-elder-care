"""Unit and integration tests for CareGuard."""

from datetime import date, datetime, timedelta
import os
import pytest

# Set environment variables before importing app components
os.environ["DATABASE_URL"] = "sqlite:///./test_careguard.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["CRON_SECRET"] = "test-cron-secret"
os.environ["SMTP_HOST"] = ""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import (
    Appointment,
    Base,
    DoseLog,
    Medication,
    Member,
    Patient,
    SessionLocal,
    engine,
)
from app.main import app
import app.notifications as notifications


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
  res_bad_token = client.get(
      "/api/me", headers={"Authorization": "Bearer bad-invalid-token"}
  )
  assert res_bad_token.status_code == 401

  # With valid token
  res_valid = client.get(
      "/api/me", headers={"Authorization": f"Bearer {valid_token}"}
  )
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
  med_id = med.id
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
  missed_emails = [
      e for e in captured_emails if "Missed dose" in e["subject"]
  ]
  assert len(missed_emails) >= 1

  appt_emails = [e for e in captured_emails if "Appointment" in e["subject"]]
  assert len(appt_emails) >= 1
  assert any("Dr. Near" in e["body"] for e in appt_emails)
  assert not any("Dr. Far" in e["body"] for e in appt_emails)

  # Verify dose became missed in DB
  db = SessionLocal()
  dose_db = (
      db.query(DoseLog).filter(DoseLog.scheduled_for == slot).first()
  )
  assert dose_db.status == "missed"
  db.close()


def test_cron_endpoint_requires_secret(client):
  """7. cron endpoint returns 403 without the secret and 200 with it."""
  # Without secret header
  res_no_secret = client.post("/api/reminders/run")
  assert res_no_secret.status_code == 403

  # With wrong secret
  res_bad_secret = client.post(
      "/api/reminders/run", headers={"X-Cron-Secret": "invalid-secret"}
  )
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

