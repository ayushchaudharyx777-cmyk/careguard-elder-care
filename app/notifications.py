"""Notification and reminder background processing for CareGuard."""

from datetime import datetime, timedelta
from email.message import EmailMessage
import logging
import smtplib
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import (
    Appointment,
    DoseLog,
    Medication,
    Member,
    Patient,
    SessionLocal,
    settings,
    utcnow,
)

logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, body: str) -> bool:
  """Send an email using configured SMTP settings, or log if SMTP_HOST is empty."""
  if not settings.SMTP_HOST:
    logger.info(
        "SMTP_HOST not set. Mock sending email to %s: [%s] %s",
        to_email,
        subject,
        body,
    )
    return True

  try:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = (
        settings.SMTP_FROM or settings.SMTP_USER or "noreply@careguard.local"
    )
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=10
    ) as server:
      if settings.SMTP_STARTTLS:
        server.starttls()
      if settings.SMTP_USER and settings.SMTP_PASSWORD:
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
      server.send_message(msg)
    return True
  except Exception as e:
    logger.error("Failed to send email to %s: %s", to_email, e)
    return False


def _notify(db: Session, patient_id: int, subject: str, message: str) -> int:
  """Send notification to all members of a patient's care circle."""
  members = db.query(Member).filter(Member.patient_id == patient_id).all()
  sent_count = 0
  for member in members:
    try:
      if send_email(member.email, subject, message):
        sent_count += 1
    except Exception as e:
      logger.error("Failed to notify member %s: %s", member.email, e)
  return sent_count


def scheduled_slots(
    medication: Medication, patient_timezone: str, ref_dt: datetime
) -> list[datetime]:
  """Compute scheduled dose slots for yesterday, today, and tomorrow in patient's time zone.

  Ignore slots before the medication's created_at.
  """
  try:
    tz = ZoneInfo(patient_timezone)
  except Exception:
    tz = ZoneInfo("UTC")

  if ref_dt.tzinfo is None:
    ref_dt_utc = ref_dt.replace(tzinfo=ZoneInfo("UTC"))
  else:
    ref_dt_utc = ref_dt.astimezone(ZoneInfo("UTC"))

  local_now = ref_dt_utc.astimezone(tz)
  local_today = local_now.date()
  target_days = [
      local_today - timedelta(days=1),
      local_today,
      local_today + timedelta(days=1),
  ]

  slots: list[datetime] = []
  for day in target_days:
    if medication.start_date and day < medication.start_date:
      continue
    if medication.end_date and day > medication.end_date:
      continue

    for time_str in medication.times:
      try:
        hour_s, min_s = time_str.split(":")
        slot_local = datetime(
            day.year, day.month, day.day, int(hour_s), int(min_s), tzinfo=tz
        )
        slot_utc = slot_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        if slot_utc < medication.created_at:
          continue
        slots.append(slot_utc)
      except Exception as e:
        logger.warning(
            "Error computing slot for time %s on %s: %s", time_str, day, e
        )

  return sorted(slots)


def _process_slot(
    db: Session,
    medication: Medication,
    slot: datetime,
    now: datetime,
    patient: Patient,
) -> tuple[int, int]:
  """Process a single scheduled dose slot.

  Returns (upcoming_count, missed_count).
  """
  upcoming_window = timedelta(minutes=settings.UPCOMING_WINDOW_MINUTES)
  missed_window = timedelta(minutes=settings.MISSED_AFTER_MINUTES)

  log = (
      db.query(DoseLog)
      .filter(
          DoseLog.medication_id == medication.id,
          DoseLog.scheduled_for == slot,
      )
      .first()
  )

  if log is None:
    if now < slot - upcoming_window:
      return 0, 0

    if now >= slot + missed_window:
      missed_log = DoseLog(
          medication_id=medication.id,
          scheduled_for=slot,
          status="missed",
          created_at=now,
          updated_at=now,
      )
      db.add(missed_log)
      subject = "Missed dose"
      body = (
          f"Missed dose alert for patient {patient.name}.\n"
          f"Medication: {medication.name} ({medication.dosage})\n"
          f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
          f"Instructions: {medication.instructions or 'None'}"
      )
      _notify(db, patient.id, subject, body)
      return 0, 1

    pending_log = DoseLog(
        medication_id=medication.id,
        scheduled_for=slot,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(pending_log)
    subject = "Medicine reminder"
    body = (
        f"Medicine reminder for patient {patient.name}.\n"
        f"Medication: {medication.name} ({medication.dosage})\n"
        f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
        f"Instructions: {medication.instructions or 'None'}"
    )
    _notify(db, patient.id, subject, body)
    return 1, 0

  if log.status == "pending" and now >= slot + missed_window:
    log.status = "missed"
    log.updated_at = now
    subject = "Missed dose"
    body = (
        f"Missed dose alert for patient {patient.name}.\n"
        f"Medication: {medication.name} ({medication.dosage})\n"
        f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
        f"Instructions: {medication.instructions or 'None'}"
    )
    _notify(db, patient.id, subject, body)
    return 0, 1

  return 0, 0


def _process_appointments(
    db: Session, patient: Patient, now: datetime
) -> int:
  """Process appointments starting in the next 24 hours.

  Returns count of notifications sent.
  """
  window_end = now + timedelta(hours=24)
  appointments = (
      db.query(Appointment)
      .filter(
          Appointment.patient_id == patient.id,
          Appointment.reminder_sent == False,
          Appointment.starts_at >= now,
          Appointment.starts_at <= window_end,
      )
      .all()
  )

  count = 0
  for appt in appointments:
    subject = "Appointment"
    body = (
        f"Upcoming appointment reminder for patient {patient.name}.\n"
        f"Doctor: {appt.doctor_name}\n"
        f"Specialty: {appt.specialty or 'General'}\n"
        f"Location: {appt.location or 'Not specified'}\n"
        f"Scheduled at (UTC): {appt.starts_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Notes: {appt.notes or 'None'}"
    )
    _notify(db, patient.id, subject, body)
    appt.reminder_sent = True
    count += 1

  return count


def run_reminder_cycle(now: datetime | None = None) -> dict[str, int]:
  """Run the reminder cycle across all patients and active medications.

  Wrap in a session and commit once. On IntegrityError, rollback and return zeros.
  """
  if now is None:
    now = utcnow()

  db: Session = SessionLocal()
  upcoming_count = 0
  missed_count = 0
  appointments_count = 0

  try:
    patients = db.query(Patient).all()
    for patient in patients:
      medications = (
          db.query(Medication)
          .filter(
              Medication.patient_id == patient.id,
              Medication.active == True,
          )
          .all()
      )
      for med in medications:
        slots = scheduled_slots(med, patient.timezone, now)
        for slot in slots:
          up, mi = _process_slot(db, med, slot, now, patient)
          upcoming_count += up
          missed_count += mi

      app_cnt = _process_appointments(db, patient, now)
      appointments_count += app_cnt

    db.commit()
    return {
        "upcoming": upcoming_count,
        "missed": missed_count,
        "appointments": appointments_count,
    }
  except IntegrityError as e:
    db.rollback()
    logger.warning("IntegrityError encountered in reminder cycle: %s", e)
    return {"upcoming": 0, "missed": 0, "appointments": 0}
  except Exception as e:
    db.rollback()
    logger.error("Unexpected error in reminder cycle: %s", e)
    raise
  finally:
    db.close()

