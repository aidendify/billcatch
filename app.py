"""BillCatch: closeout extras/T&M checklist → draft invoice lines + unbilled aging."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "billcatch-self-hosted-change-me")

app.teardown_appcontext(H.close_db)

_scheduler_lock = threading.Lock()
_scheduler_started = False


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "sms_configured": H.sms_configured(),
        "business_name": H.business_name(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "status_label": H.status_label,
        "event_label": H.event_label,
        "template_label": H.template_label,
        "format_money": H.format_money,
        "STATUS_LABELS": H.STATUS_LABELS,
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def _start_scheduler() -> None:
    global _scheduler_started
    if os.environ.get("BILLCATCH_DISABLE_SCHEDULER") == "1":
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def loop() -> None:
        while True:
            try:
                H.process_unbilled_alerts(app)
            except Exception:
                app.logger.exception("unbilled tick failed")
            time.sleep(60)

    t = threading.Thread(target=loop, name="billcatch-unbilled", daemon=True)
    t.start()


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "sms_configured": H.sms_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    try:
        H.process_unbilled_alerts(app)
    except Exception:
        app.logger.exception("unbilled check on dashboard failed")

    db = H.get_db()
    open_jobs = db.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'open'
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    unbilled = H.unbilled_jobs(db)
    recently_invoiced = db.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'invoiced'
        ORDER BY invoiced_at DESC, id DESC
        LIMIT 20
        """
    ).fetchall()
    return render_template(
        "index.html",
        open_jobs=open_jobs,
        unbilled=unbilled,
        recently_invoiced=recently_invoiced,
    )


@app.route("/jobs/new", methods=["GET", "POST"])
def new_job():
    templates = H.load_templates()
    if request.method == "GET":
        return render_template(
            "new_job.html",
            templates=templates,
            form=None,
            errors=None,
        )

    form = {
        "trade_template": (request.form.get("trade_template") or "").strip(),
        "customer_name": (request.form.get("customer_name") or "").strip(),
        "customer_email": (request.form.get("customer_email") or "").strip(),
        "customer_phone": (request.form.get("customer_phone") or "").strip(),
        "job_ref": (request.form.get("job_ref") or "").strip(),
        "title": (request.form.get("title") or "").strip(),
        "site_label": (request.form.get("site_label") or "").strip(),
        "notes": (request.form.get("notes") or "").strip(),
    }
    errors: list[str] = []
    if not form["customer_name"]:
        errors.append("Customer name is required.")
    if not form["trade_template"] or form["trade_template"] not in templates:
        errors.append("Choose a trade template.")
    if errors:
        return render_template(
            "new_job.html",
            templates=templates,
            form=form,
            errors=errors,
        ), 400

    job_id = H.create_job(
        H.get_db(),
        customer_name=form["customer_name"],
        trade_template=form["trade_template"],
        customer_email=form["customer_email"],
        customer_phone=form["customer_phone"],
        title=form["title"],
        job_ref=form["job_ref"],
        site_label=form["site_label"],
        notes=form["notes"],
    )
    flash("Job created.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<int:job_id>")
def job_detail(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    items = H.list_job_items(db, job_id)
    lines = H.list_invoice_lines(db, job_id)
    events = H.list_events(db, job_id)
    custom = None
    checklist = []
    for item in items:
        if item["item_key"] == "__custom__":
            custom = item
        else:
            checklist.append(item)
    copy_text = H.draft_copy_text(db, job_id) if lines else ""
    subtotal = sum(int(r["line_cents"]) for r in lines)
    total = subtotal + int(job["tax_cents"] or 0)
    return render_template(
        "job_detail.html",
        job=job,
        checklist=checklist,
        custom=custom,
        lines=lines,
        events=events,
        copy_text=copy_text,
        subtotal=subtotal,
        total=total,
        aging_hours=H.aging_hours(job["completed_at"]),
    )


@app.post("/jobs/<int:job_id>/complete")
def job_complete(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    try:
        H.complete_job(db, job_id, request.form)
        flash("Job marked completed. Checklist saved.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/draft")
def job_draft(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    # Save checklist first (may still be open)
    try:
        if job["status"] == "open":
            H.complete_job(db, job_id, request.form)
        else:
            H.save_checklist_from_form(db, job_id, request.form)
            db.commit()
        tax_raw = (request.form.get("tax_cents") or "").strip()
        tax_cents = int(job["tax_cents"] or 0)
        if tax_raw != "":
            try:
                if "." in tax_raw:
                    tax_cents = int(round(float(tax_raw) * 100))
                else:
                    tax_cents = int(tax_raw)
            except ValueError:
                pass
        n = H.generate_draft(db, job_id, tax_cents=tax_cents)
        flash(f"Draft generated ({n} lines).", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<int:job_id>/export.csv")
def job_export_csv(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    csv_text = H.draft_csv(db, job_id)
    if not csv_text.strip():
        flash("No draft lines to export.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    filename = f"billcatch-job-{job_id}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/jobs/<int:job_id>/invoiced")
def job_invoiced(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    try:
        H.mark_invoiced(db, job_id)
        flash("Marked invoiced.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/photo")
def job_photo(job_id: int):
    db = H.get_db()
    job = H.get_job(db, job_id)
    if not job:
        abort(404)
    file = request.files.get("photo")
    caption = (request.form.get("photo_caption") or "").strip()
    if not file or not file.filename:
        flash("Choose a photo to upload.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    try:
        # bind secure name hint but helpers picks extension
        file.filename = secure_filename(file.filename) or file.filename
        H.save_photo(job_id, file, caption=caption)
        flash("Photo saved.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        app.logger.exception("photo upload failed")
        flash("Photo upload failed.", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    # owner-gated via before_request
    return send_from_directory(H.upload_dir(), filename)


@app.post("/admin/unbilled-check")
def admin_unbilled_check():
    try:
        n = H.process_unbilled_alerts(app, force=True)
        flash(f"Unbilled check ran ({n} alerted).", "ok")
    except Exception:
        app.logger.exception("force unbilled check failed")
        flash("Unbilled check failed.", "error")
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


# Start DB + scheduler at import (gunicorn workers)
try:
    init_db()
except Exception:
    pass
_start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or "8080")
    init_db()
    _start_scheduler()
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
