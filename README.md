# BillCatch

Free, self-hosted closeout billing catch for local service owners. Run an extras/T&M checklist when the job ends, draft invoice lines, export them, and get nudged if a completed job still isn’t invoiced after 48 hours.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create a job from one of three trade templates (HVAC service, plumbing service, electrical service)
- Snapshot extras / T&M checklist onto the job at create
- Close out: fill flags / qty / hours, edit unit prices, optional one photo note
- **Generate draft** → invoice line table with **Copy**, **Export CSV**, **Mark invoiced**
- In-process tick (~60s): completed jobs still uninvoiced after `UNINVOICED_HOURS` (default 48) land on the **Unbilled** queue and optionally email/SMS the office
- Optional BYO SMTP and/or Twilio — works fully with Unbilled queue + Copy/CSV alone
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"sms_configured":false}` even when SMTP/Twilio unset

No customer Accept portal. No Stripe. No PDF renderer required.

## Privacy

Self-hosted. You run the box; the owner is the data controller for customer contact fields. No Stripe, no bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file and optional photo uploads on the Compose volume.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/billcatch.git
cd billcatch
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Set `UNINVOICED_HOURS=0` for a first smoke if desired. Leave `SMTP_*`, Twilio, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `billcatch-data` volume at `/data/billcatch.db`; uploads under `/data/uploads`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
BUSINESS_NAME=Harbor HVAC
PUBLIC_BASE_URL=http://localhost:8080
UNINVOICED_HOURS=0
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` and Twilio vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"sms_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create an **HVAC service** job. On the detail page, select at least one flag (e.g. surge protector) and enter after-hours hours. Hit **Generate draft**. Confirm the table has ≥2 lines and a total. Use **Copy** and **Export CSV**.

3. Job stays `completed` until you hit **Mark invoiced**. With `UNINVOICED_HOURS=0` (or **Force unbilled check**), a completed uninvoiced job appears under **Unbilled**; after Mark invoiced it leaves the queue.

4. Confirm empty `MARKETING_URL` shows no “Powered by” footer. Photo upload is optional — skip still drafts.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/billcatch.db`. |
| `UPLOAD_DIR` | Photo uploads. Compose overrides to `/data/uploads`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | UI copy. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in optional notify links, e.g. `http://localhost:8080`. |
| `UNINVOICED_HOURS` | Default 48. Verifier uses 0 so QA does not wait. |
| `CURRENCY` | Display currency. Default USD. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email sign-off. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email notify. If `SMTP_HOST` is unset, email notify is unused. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional SMS notify. If unset, SMS is unused. |
| `OWNER_NOTIFY_EMAIL` / `OFFICE_EMAIL` | Unbilled email destination. |
| `OFFICE_PHONE` | Unbilled SMS destination. |
| `MARKETING_URL` | If set, footer link **Powered by BillCatch** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP / Twilio secrets and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false,"sms_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `sms_configured` is `true` only when all three Twilio vars are set. Health succeeds even when both are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./data/billcatch.db
export UPLOAD_DIR=./data/uploads
export OWNER_PASSWORD=testpass
export PUBLIC_BASE_URL=http://localhost:8080
export BUSINESS_NAME="Harbor HVAC"
export UNINVOICED_HOURS=0
export BILLCATCH_DISABLE_SCHEDULER=1
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

```bash
python -m unittest test_app.py -v
```

## What this is not

BillCatch is **not** ChangeSlip (no customer magic-link Accept of change orders). It is **not** AR reminder SaaS / Stripe payment collection. It is **not** a full FSM/CRM. It is **not** QuickBooks sync. It is **not** LateBump, HomeReady, SkyHold, PartPing, OpenPing, AfterJob, FormFirst, or Nudge. It is **not** WhatsApp.

No maps, no payments, no Redis, no Celery, no required LLM, no second Compose service.
