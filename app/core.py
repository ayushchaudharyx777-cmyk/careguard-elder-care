"""Core settings, models, and utility functions for CareGuard."""

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Generator
from zoneinfo import ZoneInfo

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.constants import (
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE_SECONDS,
    DB_POOL_SIZE,
    DEFAULT_MISSED_AFTER_MINUTES,
    DEFAULT_REMINDER_INTERVAL_SECONDS,
    DEFAULT_SESSION_HOURS,
    DEFAULT_SMTP_PORT,
    DEFAULT_UPCOMING_WINDOW_MINUTES,
    PBKDF2_ITERATIONS,
    SALT_BYTES,
    SESSION_TOKEN_BYTES,
)


@dataclass
class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./careguard.db")
    CRON_SECRET: str = os.getenv("CRON_SECRET", "default-cron-secret")
    SESSION_HOURS: int = int(os.getenv("SESSION_HOURS", str(DEFAULT_SESSION_HOURS)))
    ENABLE_SCHEDULER: bool = os.getenv("ENABLE_SCHEDULER", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    REMINDER_INTERVAL_SECONDS: int = int(
        os.getenv("REMINDER_INTERVAL_SECONDS", str(DEFAULT_REMINDER_INTERVAL_SECONDS))
    )
    UPCOMING_WINDOW_MINUTES: int = int(
        os.getenv("UPCOMING_WINDOW_MINUTES", str(DEFAULT_UPCOMING_WINDOW_MINUTES))
    )
    MISSED_AFTER_MINUTES: int = int(
        os.getenv("MISSED_AFTER_MINUTES", str(DEFAULT_MISSED_AFTER_MINUTES))
    )
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")
    SMTP_STARTTLS: bool = os.getenv("SMTP_STARTTLS", "true").lower() in (
        "true",
        "1",
        "yes",
    )

    def __post_init__(self) -> None:
        if self.DATABASE_URL.startswith("postgresql+psycopg://"):
            pass
        elif self.DATABASE_URL.startswith("postgres://"):
            self.DATABASE_URL = self.DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
        elif self.DATABASE_URL.startswith("postgresql://"):
            self.DATABASE_URL = self.DATABASE_URL.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )


settings = Settings()

engine_kwargs: dict = {
    "pool_pre_ping": True,
    "pool_recycle": DB_POOL_RECYCLE_SECONDS,
}
if "sqlite" in settings.DATABASE_URL:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_size"] = DB_POOL_SIZE
    engine_kwargs["max_overflow"] = DB_MAX_OVERFLOW

engine = create_engine(settings.DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def utcnow() -> datetime:
    """Return current naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_to_utc(dt: datetime, tz_name: str) -> datetime:
    """Convert local datetime in tz_name to naive UTC datetime."""
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz_name: str) -> datetime:
    """Convert UTC naive datetime to local datetime in tz_name."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz_name))


def hash_passcode(passcode: str) -> tuple[str, str]:
    """Hash passcode with PBKDF2-SHA256 (310,000 iterations, random 16-byte salt)."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return key.hex(), salt.hex()


def verify_passcode(passcode: str, salt_hex: str, stored_hash: str) -> bool:
    """Verify passcode against salt and stored PBKDF2-SHA256 hash."""
    salt = bytes.fromhex(salt_hex)
    key = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(key.hex(), stored_hash)


def new_session_token() -> str:
    """Generate a random 32-byte session token as hex string."""
    return secrets.token_hex(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Compute SHA-256 hash of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="UTC", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    members: Mapped[list["Member"]] = relationship(
        "Member", back_populates="patient", cascade="all, delete-orphan"
    )
    medications: Mapped[list["Medication"]] = relationship(
        "Medication", back_populates="patient", cascade="all, delete-orphan"
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment", back_populates="patient", cascade="all, delete-orphan"
    )


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    passcode_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    salt: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="family", nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_members_patient_id", "patient_id"),)

    patient: Mapped["Patient"] = relationship("Patient", back_populates="members")
    sessions: Mapped[list["AuthSession"]] = relationship(
        "AuthSession", back_populates="member", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    member_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("members.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_auth_sessions_member_id", "member_id"),
        Index("ix_auth_sessions_token_expires", "token_hash", "expires_at"),
    )

    member: Mapped["Member"] = relationship("Member", back_populates="sessions")


class Medication(Base):
    __tablename__ = "medications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    dosage: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    times: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_medications_patient_id", "patient_id"),
        Index("ix_medications_patient_active", "patient_id", "active"),
    )

    patient: Mapped["Patient"] = relationship("Patient", back_populates="medications")
    doses: Mapped[list["DoseLog"]] = relationship(
        "DoseLog", back_populates="medication", cascade="all, delete-orphan"
    )


class DoseLog(Base):
    __tablename__ = "dose_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    medication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("medications.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("medication_id", "scheduled_for", name="uq_medication_scheduled_for"),
        Index("ix_dose_logs_medication_id", "medication_id"),
        Index(
            "ix_dose_logs_med_sched_status",
            "medication_id",
            "scheduled_for",
            "status",
        ),
        Index("ix_dose_logs_scheduled_for", "scheduled_for"),
        Index("ix_dose_logs_status", "status"),
    )

    medication: Mapped["Medication"] = relationship("Medication", back_populates="doses")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    doctor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialty: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_appointments_patient_starts", "patient_id", "starts_at"),
        Index("ix_appointments_starts_at", "starts_at"),
        Index("ix_appointments_reminder_sent", "reminder_sent"),
    )

    patient: Mapped["Patient"] = relationship("Patient", back_populates="appointments")


def get_db() -> Generator:
    """FastAPI dependency for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
