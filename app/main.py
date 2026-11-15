"""FastAPI application for CareGuard."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hmac
import logging
import os

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core import (
    Appointment,
    AuthSession,
    Base,
    DoseLog,
    Medication,
    Member,
    Patient,
    engine,
    get_db,
    hash_passcode,
    hash_session_token,
    local_to_utc,
    new_session_token,
    settings,
    utcnow,
    verify_passcode,
)
from app.notifications import run_reminder_cycle
from app.schemas import (
    AppointmentIn,
    AppointmentOut,
    DoseUpdate,
    LoginIn,
    MedicationIn,
    MedicationOut,
    MemberIn,
    MemberOut,
    SetupIn,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  Base.metadata.create_all(bind=engine)

  scheduler_task = None
  if settings.ENABLE_SCHEDULER:

    async def scheduler_loop():
      while True:
        try:
          await asyncio.sleep(settings.REMINDER_INTERVAL_SECONDS)
          run_reminder_cycle()
        except asyncio.CancelledError:
          break
        except Exception as e:
          logger.error("Error in scheduler loop: %s", e)

    scheduler_task = asyncio.create_task(scheduler_loop())

  yield

  if scheduler_task:
    scheduler_task.cancel()
    try:
      await scheduler_task
    except asyncio.CancelledError:
      pass


app = FastAPI(title="CareGuard API", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
  response = await call_next(request)
  response.headers["X-Content-Type-Options"] = "nosniff"
  response.headers["X-Frame-Options"] = "DENY"
  response.headers["Referrer-Policy"] = "no-referrer"
  response.headers["Content-Security-Policy"] = (
      "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src"
      " 'self'; frame-ancestors 'none'"
  )
  return response


static_dir = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "static"
)
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def get_current_member(
    request: Request, db: Session = Depends(get_db)
) -> Member:
  auth_header = request.headers.get("Authorization")
  token = None
  if auth_header and auth_header.startswith("Bearer "):
    token = auth_header[7:].strip()
  elif request.headers.get("X-Session-Token"):
    token = request.headers.get("X-Session-Token", "").strip()

  if not token:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication token required",
    )

  token_hash = hash_session_token(token)
  now = utcnow()
  session_obj = (
      db.query(AuthSession)
      .options(joinedload(AuthSession.member).joinedload(Member.patient))
      .filter(
          AuthSession.token_hash == token_hash,
          AuthSession.expires_at > now,
      )
      .first()
  )

  if not session_obj or not session_obj.member:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired session token",
    )

  return session_obj.member


@app.get("/")
def get_root():
  index_path = os.path.join(static_dir, "index.html")
  if os.path.exists(index_path):
    return FileResponse(index_path, media_type="text/html")
  return HTMLResponse("<h1>CareGuard</h1>")


@app.get("/api/health")
def get_health():
  return {"status": "ok"}


@app.get("/api/setup/status")
def get_setup_status(db: Session = Depends(get_db)):
  has_patient = db.query(Patient).first() is not None
  return {"setup_complete": has_patient}


@app.post("/api/setup", status_code=status.HTTP_201_CREATED)
def post_setup(payload: SetupIn, db: Session = Depends(get_db)):
  if db.query(Patient).first() is not None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Setup has already been completed",
    )

  now = utcnow()
  patient = Patient(
      name=payload.patient_name,
      timezone=payload.patient_timezone,
      created_at=now,
  )
  db.add(patient)
  db.flush()

  pass_hash, salt_hex = hash_passcode(payload.caregiver_passcode)
  caregiver = Member(
      patient_id=patient.id,
      name=payload.caregiver_name,
      email=payload.caregiver_email,
      passcode_hash=pass_hash,
      salt=salt_hex,
      role="caregiver",
      created_at=now,
  )
  db.add(caregiver)
  db.flush()

  raw_token = new_session_token()
  hashed_token = hash_session_token(raw_token)
  expires = now + timedelta(hours=settings.SESSION_HOURS)

  auth_session = AuthSession(
      member_id=caregiver.id,
      token_hash=hashed_token,
      expires_at=expires,
      created_at=now,
  )
  db.add(auth_session)
  db.commit()

  return {
      "patient_id": patient.id,
      "member": MemberOut.model_validate(caregiver),
      "token": raw_token,
  }


@app.post("/api/auth/login")
def post_login(payload: LoginIn, db: Session = Depends(get_db)):
  now = utcnow()
  member = db.query(Member).filter(Member.email == payload.email).first()
  if not member:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or passcode",
    )

  if member.locked_until and member.locked_until > now:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Account is locked due to too many failed attempts. Try again"
        " in 15 minutes.",
    )

  if not verify_passcode(payload.passcode, member.salt, member.passcode_hash):
    member.failed_attempts += 1
    if member.failed_attempts >= 5:
      member.locked_until = now + timedelta(minutes=15)
    db.commit()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or passcode",
    )

  member.failed_attempts = 0
  member.locked_until = None

  raw_token = new_session_token()
  hashed_token = hash_session_token(raw_token)
  expires = now + timedelta(hours=settings.SESSION_HOURS)

  auth_session = AuthSession(
      member_id=member.id,
      token_hash=hashed_token,
      expires_at=expires,
      created_at=now,
  )
  db.add(auth_session)
  db.commit()

  return {
      "token": raw_token,
      "member": MemberOut.model_validate(member),
      "expires_at": expires.isoformat(),
  }


@app.post("/api/auth/logout")
def post_logout(
    request: Request,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  auth_header = request.headers.get("Authorization")
  token = None
  if auth_header and auth_header.startswith("Bearer "):
    token = auth_header[7:].strip()
  elif request.headers.get("X-Session-Token"):
    token = request.headers.get("X-Session-Token", "").strip()

  if token:
    hashed = hash_session_token(token)
    db.query(AuthSession).filter(AuthSession.token_hash == hashed).delete()
    db.commit()

  return {"message": "Logged out successfully"}


@app.get("/api/me")
def get_me(
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  patient = current_member.patient
  return {
      "member": MemberOut.model_validate(current_member),
      "patient": {
          "id": patient.id if patient else None,
          "name": patient.name if patient else "",
          "timezone": patient.timezone if patient else "UTC",
      },
  }


@app.get("/api/members", response_model=list[MemberOut])
def get_members(
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  members = (
      db.query(Member)
      .filter(Member.patient_id == current_member.patient_id)
      .order_by(Member.id.asc())
      .all()
  )
  return [MemberOut.model_validate(m) for m in members]


@app.post(
    "/api/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
def post_members(
    payload: MemberIn,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  if current_member.role != "caregiver":
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only caregivers can add care circle members",
    )

  existing = db.query(Member).filter(Member.email == payload.email).first()
  if existing:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A member with this email already exists",
    )

  now = utcnow()
  pass_hash, salt_hex = hash_passcode(payload.passcode)
  member = Member(
      patient_id=current_member.patient_id,
      name=payload.name,
      email=payload.email,
      passcode_hash=pass_hash,
      salt=salt_hex,
      role=payload.role,
      created_at=now,
  )
  db.add(member)
  db.commit()
  db.refresh(member)
  return MemberOut.model_validate(member)


@app.delete("/api/members/{member_id}")
def delete_member(
    member_id: int,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  if current_member.role != "caregiver":
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only caregivers can remove members",
    )

  if current_member.id == member_id:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="A member cannot remove themselves",
    )

  target = (
      db.query(Member)
      .filter(
          Member.id == member_id, Member.patient_id == current_member.patient_id
      )
      .first()
  )
  if not target:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Member not found in care circle",
    )

  db.delete(target)
  db.commit()
  return {"message": "Member removed successfully"}


@app.get("/api/medications", response_model=list[MedicationOut])
def get_medications(
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  meds = (
      db.query(Medication)
      .filter(Medication.patient_id == current_member.patient_id)
      .order_by(Medication.id.asc())
      .all()
  )
  return [MedicationOut.model_validate(m) for m in meds]


@app.post(
    "/api/medications",
    response_model=MedicationOut,
    status_code=status.HTTP_201_CREATED,
)
def post_medication(
    payload: MedicationIn,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  now = utcnow()
  med = Medication(
      patient_id=current_member.patient_id,
      name=payload.name,
      dosage=payload.dosage,
      instructions=payload.instructions,
      times=payload.times,
      start_date=payload.start_date,
      end_date=payload.end_date,
      active=payload.active,
      created_at=now,
  )
  db.add(med)
  db.commit()
  db.refresh(med)
  return MedicationOut.model_validate(med)


@app.put("/api/medications/{med_id}", response_model=MedicationOut)
def put_medication(
    med_id: int,
    payload: MedicationIn,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  med = (
      db.query(Medication)
      .filter(
          Medication.id == med_id,
          Medication.patient_id == current_member.patient_id,
      )
      .first()
  )
  if not med:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Medication not found",
    )

  med.name = payload.name
  med.dosage = payload.dosage
  med.instructions = payload.instructions
  med.times = payload.times
  med.start_date = payload.start_date
  med.end_date = payload.end_date
  med.active = payload.active
  db.commit()
  db.refresh(med)
  return MedicationOut.model_validate(med)


@app.delete("/api/medications/{med_id}")
def delete_medication(
    med_id: int,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  med = (
      db.query(Medication)
      .filter(
          Medication.id == med_id,
          Medication.patient_id == current_member.patient_id,
      )
      .first()
  )
  if not med:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Medication not found",
    )

  db.query(DoseLog).filter(DoseLog.medication_id == med_id).delete()
  db.delete(med)
  db.commit()
  return {"message": "Medication and dose logs deleted successfully"}


@app.get("/api/doses")
def get_doses(
    days: int | None = Query(default=None),
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  query = (
      db.query(DoseLog)
      .options(joinedload(DoseLog.medication))
      .join(Medication, DoseLog.medication_id == Medication.id)
      .filter(Medication.patient_id == current_member.patient_id)
  )
  if days is not None:
    cutoff = utcnow() - timedelta(days=days)
    query = query.filter(DoseLog.scheduled_for >= cutoff)

  doses = query.order_by(DoseLog.scheduled_for.desc()).all()
  return [
      {
          "id": log.id,
          "medication_id": log.medication_id,
          "medication_name": log.medication.name if log.medication else "",
          "dosage": log.medication.dosage if log.medication else "",
          "instructions": log.medication.instructions if log.medication else "",
          "scheduled_for": log.scheduled_for.isoformat(),
          "status": log.status,
          "updated_at": log.updated_at.isoformat(),
          "created_at": log.created_at.isoformat(),
      }
      for log in doses
  ]


@app.patch("/api/doses/{dose_id}")
def patch_dose(
    dose_id: int,
    payload: DoseUpdate,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  log = (
      db.query(DoseLog)
      .join(Medication, DoseLog.medication_id == Medication.id)
      .filter(
          DoseLog.id == dose_id,
          Medication.patient_id == current_member.patient_id,
      )
      .first()
  )
  if not log:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Dose log not found",
    )

  log.status = payload.status
  log.updated_at = utcnow()
  db.commit()
  return {
      "id": log.id,
      "medication_id": log.medication_id,
      "scheduled_for": log.scheduled_for.isoformat(),
      "status": log.status,
      "updated_at": log.updated_at.isoformat(),
  }


@app.get("/api/adherence")
def get_adherence(
    days: int = Query(default=7, ge=1),
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  now = utcnow()
  cutoff = now - timedelta(days=days)

  status_counts = (
      db.query(DoseLog.status, func.count(DoseLog.id))
      .join(Medication, DoseLog.medication_id == Medication.id)
      .filter(
          Medication.patient_id == current_member.patient_id,
          DoseLog.scheduled_for >= cutoff,
          DoseLog.scheduled_for <= now,
      )
      .group_by(DoseLog.status)
      .all()
  )

  counts = dict(status_counts)
  taken = counts.get("taken", 0)
  skipped = counts.get("skipped", 0)
  missed = counts.get("missed", 0)
  total = taken + skipped + missed

  adherence = round((taken / total) * 100, 1) if total > 0 else None

  return {
      "days": days,
      "taken": taken,
      "skipped": skipped,
      "missed": missed,
      "total_due": total,
      "adherence": adherence,
  }


@app.get("/api/appointments", response_model=list[AppointmentOut])
def get_appointments(
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  appts = (
      db.query(Appointment)
      .filter(Appointment.patient_id == current_member.patient_id)
      .order_by(Appointment.starts_at.asc())
      .all()
  )
  return [AppointmentOut.model_validate(a) for a in appts]


@app.post(
    "/api/appointments",
    response_model=AppointmentOut,
    status_code=status.HTTP_201_CREATED,
)
def post_appointment(
    payload: AppointmentIn,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  patient = (
      db.query(Patient).filter(Patient.id == current_member.patient_id).first()
  )
  patient_tz = patient.timezone if patient else "UTC"

  starts_at_utc = local_to_utc(payload.starts_at, patient_tz)
  now = utcnow()

  appt = Appointment(
      patient_id=current_member.patient_id,
      doctor_name=payload.doctor_name,
      specialty=payload.specialty,
      location=payload.location,
      notes=payload.notes,
      starts_at=starts_at_utc,
      reminder_sent=False,
      created_at=now,
  )
  db.add(appt)
  db.commit()
  db.refresh(appt)
  return AppointmentOut.model_validate(appt)


@app.delete("/api/appointments/{appt_id}")
def delete_appointment(
    appt_id: int,
    current_member: Member = Depends(get_current_member),
    db: Session = Depends(get_db),
):
  appt = (
      db.query(Appointment)
      .filter(
          Appointment.id == appt_id,
          Appointment.patient_id == current_member.patient_id,
      )
      .first()
  )
  if not appt:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Appointment not found",
    )

  db.delete(appt)
  db.commit()
  return {"message": "Appointment deleted successfully"}


@app.post("/api/reminders/run")
def post_reminders_run(request: Request):
  secret_header = request.headers.get("X-Cron-Secret", "")
  if not secret_header or not hmac.compare_digest(
      secret_header, settings.CRON_SECRET
  ):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid cron secret header",
    )

  stats = run_reminder_cycle()
  return {"status": "ok", "stats": stats}

