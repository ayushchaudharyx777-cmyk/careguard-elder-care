# CareGuard - Elderly Care & Medication Management Platform

CareGuard is a dedicated elderly care and medication adherence platform designed for an elderly patient and their care circle (caregivers and family members).

## Features
- **First-Run Single Setup**: Initializes the patient record and the primary caregiver account once.
- **Secure Authentication**: Email and passcode authentication with PBKDF2-SHA256 (310,000 iterations, 16-byte random salt). Account automatically locks for 15 minutes after 5 consecutive failed attempts. Session tokens are 32-byte cryptographically random values, hashed with SHA-256 in the database and expiring after 12 hours.
- **Medication Scheduling & Adherence Tracking**: Schedule 24-hour medication times (`HH:MM`), track pending/taken/skipped/missed statuses, and calculate adherence percentages over customizable date ranges.
- **Automated Reminder & Missed Dose Detection**: Background cycle computes scheduled dose slots across yesterday, today, and tomorrow in the patient's local timezone. Generates reminders, tracks missed doses past 60 minutes, and triggers doctor appointment notifications within 24 hours.
- **Role-Based Care Circle**: Caregivers can invite and manage family members, while all care circle members stay informed via notifications and can log doses.
- **Accessibility-First UI**: Large 19px base font, minimum 48px interactive touch targets, visible focus rings, high-contrast palette, zero innerHTML (100% pure DOM createElement), and full ARIA support.
- **Robust Security Headers**: Enforces strict CSP without inline scripts, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.

---

## Local Development & Setup

### Prerequisites
- Python 3.12+
- `pip`

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/ayushchaudharyx777-cmyk/careguard-elder-care.git
cd careguard-elder-care

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: .\venv\Scripts\Activate.ps1

# Install test and development requirements
pip install -r requirements-dev.txt
```

### 2. Configure Environment
Copy `.env.example` to `.env` and adjust if needed:
```bash
cp .env.example .env
```

### 3. Run Test Suite
```bash
pytest -v
```

### 4. Run Locally
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Navigate to `http://localhost:8000` in your web browser.

---

## Deployment Guides

### Deploying to Render
CareGuard includes a turnkey `render.yaml` blueprint.
1. Connect this repository in your [Render Dashboard](https://dashboard.render.com).
2. Click **New +** → **Blueprint**.
3. Select this repository and click **Apply**.
4. Render provisions a managed PostgreSQL instance and deploys the FastAPI web service automatically.
5. In the Render service dashboard, add your SMTP credentials (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, etc.) to enable live email delivery.

### Deploying to Google Cloud Run
1. Ensure the Google Cloud SDK (`gcloud`) is installed and authenticated:
   ```bash
   gcloud auth login
   gcloud config set project <PROJECT_ID>
   ```
2. Enable required Google Cloud services:
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com --quiet
   ```
3. Deploy the container from source to Cloud Run:
   ```bash
   gcloud run deploy careguard \
     --source . \
     --region asia-south1 \
     --allow-unauthenticated \
     --max-instances 1 \
     --set-env-vars "ENABLE_SCHEDULER=true,CRON_SECRET=<RANDOM_32_CHAR_SECRET>" \
     --quiet
   ```
4. Create a recurring Cloud Scheduler job to invoke the reminder endpoint every 5 minutes:
   ```bash
   gcloud scheduler jobs create http careguard-reminders \
     --location asia-south1 \
     --schedule "*/5 * * * *" \
     --uri "<CLOUD_RUN_SERVICE_URL>/api/reminders/run" \
     --http-method POST \
     --headers "X-Cron-Secret=<RANDOM_32_CHAR_SECRET>" \
     --quiet
   ```

---

## API Reference

| Method | Endpoint | Description | Auth Required |
|---|---|---|---|
| `GET` | `/` | Serves accessible single-page front end | No |
| `GET` | `/api/health` | Health check endpoint | No |
| `GET` | `/api/setup/status` | Check if initial setup is complete | No |
| `POST` | `/api/setup` | First-run patient & caregiver creation (works once) | No |
| `POST` | `/api/auth/login` | Authenticate with email & passcode (locks at 5 fails) | No |
| `POST` | `/api/auth/logout` | Invalidate current session token | Yes |
| `GET` | `/api/me` | Fetch active user profile and patient info | Yes |
| `GET` | `/api/members` | List members of the care circle | Yes |
| `POST` | `/api/members` | Add family/caregiver member (Caregivers only) | Yes (Caregiver) |
| `DELETE` | `/api/members/{id}` | Remove a member (Caregivers only; cannot remove self) | Yes (Caregiver) |
| `GET` | `/api/medications` | List patient's medications | Yes |
| `POST` | `/api/medications` | Add medication with sorted, unique `HH:MM` times | Yes |
| `PUT` | `/api/medications/{id}` | Update existing medication | Yes |
| `DELETE` | `/api/medications/{id}` | Delete medication and associated dose logs | Yes |
| `GET` | `/api/doses?days=` | List scheduled doses within the past N days | Yes |
| `PATCH` | `/api/doses/{id}` | Mark dose as `taken` or `skipped` | Yes |
| `GET` | `/api/adherence?days=` | Calculate medication adherence rate over N days | Yes |
| `GET` | `/api/appointments` | List medical appointments | Yes |
| `POST` | `/api/appointments` | Create appointment (converts local patient tz to UTC) | Yes |
| `DELETE` | `/api/appointments/{id}` | Delete/cancel appointment | Yes |
| `POST` | `/api/reminders/run` | Trigger reminder cycle via cron (`X-Cron-Secret` header) | Secret Header |

