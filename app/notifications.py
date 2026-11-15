"""Notification and reminder background processing for CareGuard."""

from datetime import datetime, timedelta
from email.message import EmailMessage
import logging
import smtplib
import threading
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

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

# Single scheduler instance lock to prevent duplicate overlapping cycle executions
_cycle_lock = threading.Lock()


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
  """Process a single scheduled dose slot (fallback/unit helper).

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
  """Process appointments starting in the next 24 hours (fallback/unit helper).

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
  """Run the bulk reminder cycle across all patients, active medications, and appointments.

  Bulk-loads medications, members, existing dose logs and appointments in single queries,
  computes status updates in memory, and bulk inserts new logs.
  Protected by a single-instance lock to prevent overlapping runs.
  Idempotent: running repeatedly produces no duplicate logs or notifications.
  """
  if not _cycle_lock.acquire(blocking=False):
    logger.warning("Reminder cycle is already executing; skipping duplicate execution.")
    return {"upcoming": 0, "missed": 0, "appointments": 0}

  try:
    if now is None:
      now = utcnow()

    db: Session = SessionLocal()
    upcoming_count = 0
    missed_count = 0
    appointments_count = 0

    try:
      # Bulk load patients along with members and active medications
      patients = (
          db.query(Patient)
          .options(
              selectinload(Patient.members),
              selectinload(Patient.medications),
          )
          .all()
      )
      if not patients:
        return {"upcoming": 0, "missed": 0, "appointments": 0}

      patient_map: dict[int, Patient] = {p.id: p for p in patients}

      # Compute slots for all active medications
      candidate_slots_by_med: dict[int, list[datetime]] = {}
      med_map: dict[int, tuple[Medication, Patient]] = {}
      all_med_ids: list[int] = []
      all_slots_set: set[datetime] = set()

      for patient in patients:
        for med in patient.medications:
          if not med.active:
            continue
          slots = scheduled_slots(med, patient.timezone, now)
          if slots:
            candidate_slots_by_med[med.id] = slots
            med_map[med.id] = (med, patient)
            all_med_ids.append(med.id)
            all_slots_set.update(slots)

      # Check upcoming appointments starting within the next 24 hours
      window_end = now + timedelta(hours=24)
      patient_ids = list(patient_map.keys())
      appointments = (
          db.query(Appointment)
          .filter(
              Appointment.patient_id.in_(patient_ids),
              Appointment.reminder_sent == False,
              Appointment.starts_at >= now,
              Appointment.starts_at <= window_end,
          )
          .all()
      )

      # Early exit if there are neither candidate slots nor appointments
      if not all_med_ids and not appointments:
        return {"upcoming": 0, "missed": 0, "appointments": 0}

      # Bulk load existing dose logs for all candidate medications and slots
      existing_log_map: dict[tuple[int, datetime], DoseLog] = {}
      if all_med_ids and all_slots_set:
        existing_logs = (
            db.query(DoseLog)
            .filter(
                DoseLog.medication_id.in_(all_med_ids),
                DoseLog.scheduled_for.in_(list(all_slots_set)),
            )
            .all()
        )
        for log in existing_logs:
          existing_log_map[(log.medication_id, log.scheduled_for)] = log

      upcoming_window = timedelta(minutes=settings.UPCOMING_WINDOW_MINUTES)
      missed_window = timedelta(minutes=settings.MISSED_AFTER_MINUTES)
      notifications_to_send: list[tuple[Patient, str, str]] = []
      new_logs: list[DoseLog] = []

      # In-memory slot evaluation
      for med_id, slots in candidate_slots_by_med.items():
        med, patient = med_map[med_id]
        for slot in slots:
          log = existing_log_map.get((med_id, slot))
          if log is None:
            if now < slot - upcoming_window:
              continue

            if now >= slot + missed_window:
              missed_log = DoseLog(
                  medication_id=med.id,
                  scheduled_for=slot,
                  status="missed",
                  created_at=now,
                  updated_at=now,
              )
              new_logs.append(missed_log)
              existing_log_map[(med_id, slot)] = missed_log
              subject = "Missed dose"
              body = (
                  f"Missed dose alert for patient {patient.name}.\n"
                  f"Medication: {med.name} ({med.dosage})\n"
                  f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
                  f"Instructions: {med.instructions or 'None'}"
              )
              notifications_to_send.append((patient, subject, body))
              missed_count += 1
            else:
              pending_log = DoseLog(
                  medication_id=med.id,
                  scheduled_for=slot,
                  status="pending",
                  created_at=now,
                  updated_at=now,
              )
              new_logs.append(pending_log)
              existing_log_map[(med_id, slot)] = pending_log
              subject = "Medicine reminder"
              body = (
                  f"Medicine reminder for patient {patient.name}.\n"
                  f"Medication: {med.name} ({med.dosage})\n"
                  f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
                  f"Instructions: {med.instructions or 'None'}"
              )
              notifications_to_send.append((patient, subject, body))
              upcoming_count += 1

          elif log.status == "pending" and now >= slot + missed_window:
            log.status = "missed"
            log.updated_at = now
            subject = "Missed dose"
            body = (
                f"Missed dose alert for patient {patient.name}.\n"
                f"Medication: {med.name} ({med.dosage})\n"
                f"Scheduled (UTC): {slot.strftime('%Y-%m-%d %H:%M')}\n"
                f"Instructions: {med.instructions or 'None'}"
            )
            notifications_to_send.append((patient, subject, body))
            missed_count += 1

      if new_logs:
        db.add_all(new_logs)

      # Process appointments
      for appt in appointments:
        p = patient_map.get(appt.patient_id)
        if not p:
          continue
        subject = "Appointment"
        body = (
            f"Upcoming appointment reminder for patient {p.name}.\n"
            f"Doctor: {appt.doctor_name}\n"
            f"Specialty: {appt.specialty or 'General'}\n"
            f"Location: {appt.location or 'Not specified'}\n"
            f"Scheduled at (UTC): {appt.starts_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"Notes: {appt.notes or 'None'}"
        )
        notifications_to_send.append((p, subject, body))
        appt.reminder_sent = True
        appointments_count += 1

      db.commit()

      # Send notifications to preloaded members
      for p, subj, bdy in notifications_to_send:
        for member in p.members:
          try:
            send_email(member.email, subj, bdy)
          except Exception as e:
            logger.error("Failed to notify member %s: %s", member.email, e)

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
  finally:
    _cycle_lock.release()
