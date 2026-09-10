"""BillCatch helpers: DB, templates, notify, unbilled aging."""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import smtplib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from flask import g

APP_ROOT = Path(__file__).resolve().parent
CHECKLISTS_DIR = APP_ROOT / "checklists"
DEFAULT_DB = str(APP_ROOT / "data" / "billcatch.db")

STATUSES = ("open", "completed", "invoiced", "cancelled")
STATUS_LABELS = {
    "open": "Open",
    "completed": "Completed",
    "invoiced": "Invoiced",
    "cancelled": "Cancelled",
}
EVENT_LABELS = {
    "created": "Created",
    "completed": "Completed",
    "drafted": "Draft generated",
    "invoiced": "Marked invoiced",
    "unbilled_alert": "Unbilled alert",
    "cancelled": "Cancelled",
    "photo": "Photo uploaded",
}

OPEN_ENDPOINTS = {"health", "login", "static"}

_template_cache: dict[str, dict] | None = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    return _env("DATABASE_PATH") or DEFAULT_DB


def upload_dir() -> Path:
    raw = _env("UPLOAD_DIR") or str(APP_ROOT / "data" / "uploads")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def owner_password() -> str:
    return _env("OWNER_PASSWORD")


def business_name() -> str:
    return _env("BUSINESS_NAME") or "BillCatch"


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def currency() -> str:
    return _env("CURRENCY") or "USD"


def uninvoiced_hours() -> float:
    raw = _env("UNINVOICED_HOURS") or "48"
    try:
        return float(raw)
    except ValueError:
        return 48.0


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect_db(path: str | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_db()
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY,
          customer_name TEXT NOT NULL,
          customer_email TEXT,
          customer_phone TEXT,
          title TEXT,
          job_ref TEXT,
          trade_template TEXT NOT NULL,
          site_label TEXT,
          notes TEXT,
          status TEXT NOT NULL,
          completed_at TEXT,
          invoiced_at TEXT,
          unbilled_alerted_at TEXT,
          tax_cents INTEGER NOT NULL DEFAULT 0,
          photo_path TEXT,
          photo_caption TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_checklist_items (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id),
          item_key TEXT NOT NULL,
          label TEXT NOT NULL,
          kind TEXT NOT NULL,
          unit_label TEXT,
          unit_cents INTEGER,
          sort_order INTEGER NOT NULL,
          selected INTEGER NOT NULL DEFAULT 0,
          quantity REAL,
          line_note TEXT
        );

        CREATE TABLE IF NOT EXISTS invoice_lines (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id),
          description TEXT NOT NULL,
          quantity REAL NOT NULL,
          unit_cents INTEGER NOT NULL,
          line_cents INTEGER NOT NULL,
          source_item_key TEXT,
          sort_order INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id),
          kind TEXT NOT NULL,
          at TEXT NOT NULL,
          meta_json TEXT
        );
        """
    )
    db.commit()


def clear_template_cache() -> None:
    global _template_cache
    _template_cache = None


def load_templates() -> dict[str, dict]:
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    templates: dict[str, dict] = {}
    if CHECKLISTS_DIR.is_dir():
        for path in sorted(CHECKLISTS_DIR.glob("*.json")):
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            key = data.get("key") or path.stem
            data["key"] = key
            templates[key] = data
    _template_cache = templates
    return templates


def get_template(key: str) -> dict | None:
    return load_templates().get(key)


def template_keys() -> list[str]:
    return list(load_templates().keys())


def template_label(key: str) -> str:
    tmpl = get_template(key)
    if tmpl:
        return tmpl.get("label") or key
    return key


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def event_label(kind: str) -> str:
    return EVENT_LABELS.get(kind, kind)


def format_money(cents: int | None) -> str:
    value = (cents or 0) / 100.0
    cur = currency()
    if cur.upper() == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f} {cur}"


def add_event(db: sqlite3.Connection, job_id: int, kind: str, meta: dict | None = None) -> None:
    db.execute(
        "INSERT INTO events (job_id, kind, at, meta_json) VALUES (?, ?, ?, ?)",
        (job_id, kind, utc_now_iso(), json.dumps(meta) if meta else None),
    )


def list_events(db: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()


def get_job(db: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_job_items(db: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM job_checklist_items
        WHERE job_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (job_id,),
    ).fetchall()


def list_invoice_lines(db: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM invoice_lines
        WHERE job_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (job_id,),
    ).fetchall()


def create_job(
    db: sqlite3.Connection,
    *,
    customer_name: str,
    trade_template: str,
    customer_email: str = "",
    customer_phone: str = "",
    title: str = "",
    job_ref: str = "",
    site_label: str = "",
    notes: str = "",
) -> int:
    tmpl = get_template(trade_template)
    if not tmpl:
        raise ValueError("Unknown trade template.")
    now = utc_now_iso()
    cur = db.execute(
        """
        INSERT INTO jobs (
          customer_name, customer_email, customer_phone, title, job_ref,
          trade_template, site_label, notes, status, tax_cents,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?)
        """,
        (
            customer_name,
            customer_email or None,
            customer_phone or None,
            title or None,
            job_ref or None,
            trade_template,
            site_label or None,
            notes or None,
            now,
            now,
        ),
    )
    job_id = int(cur.lastrowid)
    for item in tmpl.get("items") or []:
        db.execute(
            """
            INSERT INTO job_checklist_items (
              job_id, item_key, label, kind, unit_label, unit_cents,
              sort_order, selected, quantity, line_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                job_id,
                item["item_key"],
                item["label"],
                item["kind"],
                item.get("unit_label"),
                item.get("default_unit_cents"),
                int(item.get("sort_order") or 0),
            ),
        )
    add_event(db, job_id, "created", {"trade_template": trade_template})
    db.commit()
    return job_id


def save_checklist_from_form(db: sqlite3.Connection, job_id: int, form) -> None:
    items = list_job_items(db, job_id)
    for item in items:
        iid = item["id"]
        kind = item["kind"]
        unit_raw = (form.get(f"unit_cents_{iid}") or "").strip()
        unit_cents = item["unit_cents"]
        if unit_raw != "":
            try:
                # allow dollars or cents: if contains '.', treat as dollars
                if "." in unit_raw:
                    unit_cents = int(round(float(unit_raw) * 100))
                else:
                    unit_cents = int(unit_raw)
            except ValueError:
                pass
        selected = 0
        quantity = None
        if kind == "flag":
            selected = 1 if form.get(f"selected_{iid}") else 0
            quantity = 1.0 if selected else None
        else:
            qty_raw = (form.get(f"quantity_{iid}") or "").strip()
            if qty_raw:
                try:
                    quantity = float(qty_raw)
                except ValueError:
                    quantity = None
                selected = 1 if quantity and quantity > 0 else 0
            else:
                selected = 0
                quantity = None
        note = (form.get(f"note_{iid}") or "").strip() or None
        db.execute(
            """
            UPDATE job_checklist_items
            SET selected = ?, quantity = ?, unit_cents = ?, line_note = ?
            WHERE id = ? AND job_id = ?
            """,
            (selected, quantity, unit_cents, note, iid, job_id),
        )
    # custom line (one per job) — stored as item_key=__custom__
    custom = db.execute(
        "SELECT id FROM job_checklist_items WHERE job_id = ? AND item_key = '__custom__'",
        (job_id,),
    ).fetchone()
    custom_label = (form.get("custom_label") or "").strip()
    custom_kind = (form.get("custom_kind") or "flag").strip()
    if custom_kind not in ("flag", "qty", "hours"):
        custom_kind = "flag"
    custom_unit_raw = (form.get("custom_unit_cents") or "").strip()
    custom_qty_raw = (form.get("custom_quantity") or "").strip()
    custom_unit = 0
    if custom_unit_raw:
        try:
            if "." in custom_unit_raw:
                custom_unit = int(round(float(custom_unit_raw) * 100))
            else:
                custom_unit = int(custom_unit_raw)
        except ValueError:
            custom_unit = 0
    custom_qty = None
    custom_selected = 0
    if custom_label:
        if custom_kind == "flag":
            custom_selected = 1 if form.get("custom_selected") else 0
            custom_qty = 1.0 if custom_selected else None
        else:
            if custom_qty_raw:
                try:
                    custom_qty = float(custom_qty_raw)
                except ValueError:
                    custom_qty = None
                custom_selected = 1 if custom_qty and custom_qty > 0 else 0
        if custom:
            db.execute(
                """
                UPDATE job_checklist_items
                SET label = ?, kind = ?, unit_cents = ?, selected = ?, quantity = ?,
                    unit_label = ?
                WHERE id = ?
                """,
                (
                    custom_label,
                    custom_kind,
                    custom_unit,
                    custom_selected,
                    custom_qty,
                    "hr" if custom_kind == "hours" else "ea",
                    custom["id"],
                ),
            )
        else:
            db.execute(
                """
                INSERT INTO job_checklist_items (
                  job_id, item_key, label, kind, unit_label, unit_cents,
                  sort_order, selected, quantity, line_note
                ) VALUES (?, '__custom__', ?, ?, ?, ?, 9999, ?, ?, NULL)
                """,
                (
                    job_id,
                    custom_label,
                    custom_kind,
                    "hr" if custom_kind == "hours" else "ea",
                    custom_unit,
                    custom_selected,
                    custom_qty,
                ),
            )
    elif custom:
        db.execute("DELETE FROM job_checklist_items WHERE id = ?", (custom["id"],))


def complete_job(db: sqlite3.Connection, job_id: int, form) -> None:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Job not found")
    if job["status"] == "cancelled":
        raise ValueError("Cancelled jobs cannot be completed")
    if job["status"] == "invoiced":
        raise ValueError("Already invoiced")
    save_checklist_from_form(db, job_id, form)
    now = utc_now_iso()
    completed_at = job["completed_at"] or now
    db.execute(
        """
        UPDATE jobs
        SET status = 'completed', completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (completed_at, now, job_id),
    )
    if job["status"] != "completed":
        add_event(db, job_id, "completed")
    db.commit()


def generate_draft(db: sqlite3.Connection, job_id: int, tax_cents: int | None = None) -> int:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Job not found")
    if job["status"] not in ("completed", "open"):
        # allow draft from completed; if still open, auto-complete path should have run
        if job["status"] == "cancelled":
            raise ValueError("Cancelled")
        if job["status"] == "invoiced":
            raise ValueError("Already invoiced")
    items = list_job_items(db, job_id)
    lines: list[tuple] = []
    sort_order = 10
    for item in items:
        if not item["selected"]:
            continue
        qty = item["quantity"] if item["quantity"] is not None else 0
        if item["kind"] == "flag":
            qty = 1.0
        if not qty or qty <= 0:
            continue
        unit = int(item["unit_cents"] or 0)
        line_cents = int(round(qty * unit))
        desc = item["label"]
        if item["line_note"]:
            desc = f"{desc} — {item['line_note']}"
        lines.append((desc, float(qty), unit, line_cents, item["item_key"], sort_order))
        sort_order += 10
    if not lines:
        raise ValueError("Select at least one checklist item (or custom line) before drafting.")
    db.execute("DELETE FROM invoice_lines WHERE job_id = ?", (job_id,))
    for desc, qty, unit, line_cents, key, so in lines:
        db.execute(
            """
            INSERT INTO invoice_lines (
              job_id, description, quantity, unit_cents, line_cents,
              source_item_key, sort_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, desc, qty, unit, line_cents, key, so),
        )
    now = utc_now_iso()
    if tax_cents is None:
        tax_cents = int(job["tax_cents"] or 0)
    # ensure completed
    completed_at = job["completed_at"] or now
    status = job["status"]
    if status == "open":
        status = "completed"
    db.execute(
        """
        UPDATE jobs
        SET tax_cents = ?, status = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (tax_cents, status, completed_at, now, job_id),
    )
    add_event(db, job_id, "drafted", {"lines": len(lines)})
    db.commit()
    return len(lines)


def draft_total_cents(db: sqlite3.Connection, job_id: int) -> int:
    job = get_job(db, job_id)
    lines = list_invoice_lines(db, job_id)
    sub = sum(int(r["line_cents"]) for r in lines)
    tax = int(job["tax_cents"] or 0) if job else 0
    return sub + tax


def draft_copy_text(db: sqlite3.Connection, job_id: int) -> str:
    job = get_job(db, job_id)
    lines = list_invoice_lines(db, job_id)
    if not job or not lines:
        return ""
    parts = [
        f"{business_name()} — draft invoice lines",
        f"Customer: {job['customer_name']}",
    ]
    if job["job_ref"]:
        parts.append(f"Job ref: {job['job_ref']}")
    if job["title"]:
        parts.append(f"Title: {job['title']}")
    if job["site_label"]:
        parts.append(f"Site: {job['site_label']}")
    parts.append("")
    parts.append("Description\tQty\tUnit\tLine")
    for row in lines:
        parts.append(
            f"{row['description']}\t{row['quantity']}\t"
            f"{format_money(row['unit_cents'])}\t{format_money(row['line_cents'])}"
        )
    tax = int(job["tax_cents"] or 0)
    sub = sum(int(r["line_cents"]) for r in lines)
    parts.append("")
    parts.append(f"Subtotal\t{format_money(sub)}")
    parts.append(f"Tax\t{format_money(tax)}")
    parts.append(f"Total\t{format_money(sub + tax)}")
    return "\n".join(parts)


def draft_csv(db: sqlite3.Connection, job_id: int) -> str:
    job = get_job(db, job_id)
    lines = list_invoice_lines(db, job_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["description", "quantity", "unit_cents", "line_cents", "source_item_key"])
    for row in lines:
        writer.writerow(
            [
                row["description"],
                row["quantity"],
                row["unit_cents"],
                row["line_cents"],
                row["source_item_key"] or "",
            ]
        )
    tax = int(job["tax_cents"] or 0) if job else 0
    sub = sum(int(r["line_cents"]) for r in lines)
    writer.writerow([])
    writer.writerow(["subtotal", "", "", sub, ""])
    writer.writerow(["tax", "", "", tax, ""])
    writer.writerow(["total", "", "", sub + tax, ""])
    return buf.getvalue()


def mark_invoiced(db: sqlite3.Connection, job_id: int) -> None:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Job not found")
    now = utc_now_iso()
    db.execute(
        """
        UPDATE jobs
        SET status = 'invoiced', invoiced_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, job_id),
    )
    add_event(db, job_id, "invoiced")
    db.commit()


def aging_hours(completed_at: str | None) -> float | None:
    dt = parse_iso(completed_at)
    if not dt:
        return None
    delta = utc_now() - dt
    return round(delta.total_seconds() / 3600.0, 1)


def unbilled_jobs(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'completed' AND invoiced_at IS NULL
        ORDER BY completed_at ASC, id ASC
        """
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["aging_hours"] = aging_hours(row["completed_at"])
        out.append(d)
    return out


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def send_twilio_sms(to_phone: str, body: str) -> None:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise RuntimeError("Twilio is not configured.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode(
        {"To": to_phone, "From": from_number, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Twilio HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Twilio HTTP {exc.code}") from exc


def notify_unbilled(job: sqlite3.Row) -> dict[str, Any]:
    meta: dict[str, Any] = {"email": False, "sms": False}
    base = public_base_url()
    link = f"{base}/jobs/{job['id']}" if base else f"/jobs/{job['id']}"
    subject = f"[BillCatch] Unbilled job: {job['customer_name']}"
    body = (
        f"Completed job still has no invoice.\n\n"
        f"Customer: {job['customer_name']}\n"
        f"Trade: {template_label(job['trade_template'])}\n"
        f"Completed at: {job['completed_at']}\n"
        f"Open: {link}\n"
    )
    to_email = _env("OWNER_NOTIFY_EMAIL") or _env("OFFICE_EMAIL")
    if smtp_configured() and to_email:
        try:
            send_smtp(to_email, subject, body)
            meta["email"] = True
        except Exception as exc:  # noqa: BLE001
            meta["email_error"] = str(exc)
    office_phone = _env("OFFICE_PHONE")
    if sms_configured() and office_phone:
        sms_body = (
            f"BillCatch unbilled: {job['customer_name']} "
            f"({template_label(job['trade_template'])}). {link}"
        )
        try:
            send_twilio_sms(office_phone, sms_body[:1500])
            meta["sms"] = True
        except Exception as exc:  # noqa: BLE001
            meta["sms_error"] = str(exc)
    return meta


def process_unbilled_alerts(app=None, force: bool = False) -> int:
    """Find completed uninvoiced jobs past threshold; alert once."""
    db = connect_db()
    try:
        init_schema(db)
        hours = uninvoiced_hours()
        cutoff = utc_now() - timedelta(hours=hours)
        rows = db.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'completed'
              AND invoiced_at IS NULL
              AND unbilled_alerted_at IS NULL
              AND completed_at IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        count = 0
        for job in rows:
            completed = parse_iso(job["completed_at"])
            if not completed:
                continue
            if not force and completed > cutoff:
                continue
            now = utc_now_iso()
            meta = notify_unbilled(job)
            db.execute(
                "UPDATE jobs SET unbilled_alerted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job["id"]),
            )
            add_event(db, job["id"], "unbilled_alert", meta)
            count += 1
        db.commit()
        return count
    finally:
        db.close()


def save_photo(job_id: int, file_storage, caption: str = "") -> str:
    """Save one image; return relative filename stored in photo_path."""
    filename = file_storage.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        raise ValueError("Photo must be jpg, png, gif, or webp.")
    dest_name = f"job_{job_id}{ext}"
    dest = upload_dir() / dest_name
    file_storage.save(dest)
    db = get_db()
    now = utc_now_iso()
    db.execute(
        """
        UPDATE jobs
        SET photo_path = ?, photo_caption = ?, updated_at = ?
        WHERE id = ?
        """,
        (dest_name, caption or None, now, job_id),
    )
    add_event(db, job_id, "photo", {"path": dest_name})
    db.commit()
    return dest_name
