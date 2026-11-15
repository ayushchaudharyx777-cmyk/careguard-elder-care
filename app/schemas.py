"""Pydantic schemas for request validation and response serialization."""

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator, model_validator

TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SetupIn(BaseModel):
    patient_name: str
    patient_timezone: str = "UTC"
    caregiver_name: str
    caregiver_email: EmailStr
    caregiver_passcode: str

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "patient_timezone" not in data and "timezone" in data:
                data["patient_timezone"] = data["timezone"]
            if "patient_name" not in data and "patient" in data:
                data["patient_name"] = data["patient"]
            if "caregiver_name" not in data and "name" in data:
                data["caregiver_name"] = data["name"]
            if "caregiver_email" not in data and "email" in data:
                data["caregiver_email"] = data["email"]
            if "caregiver_passcode" not in data and "passcode" in data:
                data["caregiver_passcode"] = data["passcode"]
        return data

    @field_validator("patient_timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"Invalid timezone: {v}")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    passcode: str


class MemberIn(BaseModel):
    name: str
    email: EmailStr
    passcode: str
    role: str = "family"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        s = v.lower()
        if s not in {"caregiver", "family", "patient"}:
            raise ValueError("Role must be 'caregiver', 'family', or 'patient'")
        return s


class MemberOut(BaseModel):
    id: int
    patient_id: int
    name: str
    email: EmailStr
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MedicationIn(BaseModel):
    name: str
    dosage: str
    instructions: str = ""
    times: list[str]
    start_date: date
    end_date: date | None = None
    active: bool = True

    @field_validator("times")
    @classmethod
    def validate_times(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("Times list cannot be empty")
        for t in v:
            if not isinstance(t, str) or not TIME_REGEX.match(t):
                raise ValueError(
                    f"Invalid time format '{t}'. Must be 24-hour HH:MM format (e.g. '08:00')."
                )
        return sorted(list(set(v)))


class MedicationOut(BaseModel):
    id: int
    patient_id: int
    name: str
    dosage: str
    instructions: str
    times: list[str]
    start_date: date
    end_date: date | None
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DoseUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        s = v.lower()
        if s not in {"taken", "skipped"}:
            raise ValueError("Status must be 'taken' or 'skipped'")
        return s


class DoseLogOut(BaseModel):
    id: int
    medication_id: int
    scheduled_for: datetime
    status: str
    updated_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AppointmentIn(BaseModel):
    doctor_name: str
    specialty: str = ""
    location: str = ""
    notes: str = ""
    starts_at: datetime


class AppointmentOut(BaseModel):
    id: int
    patient_id: int
    doctor_name: str
    specialty: str
    location: str
    notes: str
    starts_at: datetime
    reminder_sent: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
