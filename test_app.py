"""BillCatch local verifier-style tests (PRD §12)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Env before import
_TMP = tempfile.mkdtemp(prefix="billcatch-test-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["UPLOAD_DIR"] = str(Path(_TMP) / "uploads")
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["UNINVOICED_HOURS"] = "0"
os.environ["MARKETING_URL"] = ""
os.environ["SECRET_KEY"] = "test-secret"
os.environ["BILLCATCH_DISABLE_SCHEDULER"] = "1"
for k in (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "OWNER_NOTIFY_EMAIL",
    "OFFICE_EMAIL",
    "OFFICE_PHONE",
):
    os.environ.pop(k, None)

import app as app_module  # noqa: E402
import helpers as H  # noqa: E402


class BillCatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H.clear_template_cache()
        Path(os.environ["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        # fresh DB each test
        db_path = os.environ["DATABASE_PATH"]
        for suffix in ("", "-wal", "-shm"):
            p = Path(db_path + suffix)
            if p.exists():
                p.unlink()
        with self.app.app_context():
            H.init_schema(H.get_db())

    def _login(self):
        return self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )

    def test_01_health_public_ok_smtp_false(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["sms_configured"], False)

    def test_02_auth_gates_dashboard_health_public(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        r2 = self.client.get("/health")
        self.assertEqual(r2.status_code, 200)

    def test_03_create_hvac_closeout_shows_extras_hours(self):
        self._login()
        r = self.client.post(
            "/jobs/new",
            data={
                "customer_name": "Ada Customer",
                "trade_template": "hvac_service",
                "job_ref": "J-100",
                "title": "Tune-up",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertIn("/jobs/", loc)
        detail = self.client.get(loc)
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn("Surge protector", html)
        self.assertIn("After-hours T&amp;M", html)
        self.assertIn("Extra filter", html)
        self.assertIn("name=\"quantity_", html)  # hours/qty fields

    def test_04_draft_two_lines_copy_csv_onclick(self):
        self._login()
        with self.app.app_context():
            job_id = H.create_job(
                H.get_db(),
                customer_name="Bob",
                trade_template="hvac_service",
            )
            items = H.list_job_items(H.get_db(), job_id)
        # Find surge (flag) and after_hours (hours)
        form = {}
        for item in items:
            if item["item_key"] == "surge_protector":
                form[f"selected_{item['id']}"] = "1"
                form[f"unit_cents_{item['id']}"] = "125.00"
            if item["item_key"] == "after_hours_tm":
                form[f"quantity_{item['id']}"] = "2"
                form[f"unit_cents_{item['id']}"] = "150.00"
        form["tax_cents"] = "0"
        r = self.client.post(f"/jobs/{job_id}/draft", data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Draft generated", html)
        self.assertIn("Surge protector", html)
        self.assertIn("After-hours T&amp;M", html)
        # Copy must not use truncated double-quoted onclick with tojson
        self.assertNotIn('onclick="copyText(', html)
        self.assertIn("copyFrom", html)
        self.assertIn('id="draft-copy"', html)
        # CSV
        csv_r = self.client.get(f"/jobs/{job_id}/export.csv")
        self.assertEqual(csv_r.status_code, 200)
        body = csv_r.get_data(as_text=True)
        self.assertIn("Surge protector", body)
        self.assertIn("After-hours", body)
        with self.app.app_context():
            lines = H.list_invoice_lines(H.get_db(), job_id)
            self.assertGreaterEqual(len(lines), 2)
            total = H.draft_total_cents(H.get_db(), job_id)
            self.assertGreater(total, 0)

    def test_05_completed_until_mark_invoiced(self):
        self._login()
        with self.app.app_context():
            job_id = H.create_job(
                H.get_db(),
                customer_name="Cara",
                trade_template="plumbing_service",
            )
            items = H.list_job_items(H.get_db(), job_id)
        form = {}
        for item in items:
            if item["item_key"] == "junk_fee":
                form[f"selected_{item['id']}"] = "1"
            if item["item_key"] == "after_hours_hours":
                form[f"quantity_{item['id']}"] = "1.5"
                form[f"unit_cents_{item['id']}"] = "145.00"
        self.client.post(f"/jobs/{job_id}/draft", data=form, follow_redirects=True)
        with self.app.app_context():
            job = H.get_job(H.get_db(), job_id)
            self.assertEqual(job["status"], "completed")
            self.assertIsNone(job["invoiced_at"])
        self.client.post(f"/jobs/{job_id}/invoiced", follow_redirects=True)
        with self.app.app_context():
            job = H.get_job(H.get_db(), job_id)
            self.assertEqual(job["status"], "invoiced")
            self.assertIsNotNone(job["invoiced_at"])

    def test_06_unbilled_queue_and_leaves_after_invoiced(self):
        self._login()
        with self.app.app_context():
            job_id = H.create_job(
                H.get_db(),
                customer_name="Dana",
                trade_template="electrical_service",
            )
            items = H.list_job_items(H.get_db(), job_id)
        form = {}
        for item in items:
            if item["item_key"] == "gfci_upgrade":
                form[f"selected_{item['id']}"] = "1"
        self.client.post(f"/jobs/{job_id}/draft", data=form, follow_redirects=True)
        # Force check / dashboard with UNINVOICED_HOURS=0
        r = self.client.post("/admin/unbilled-check", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        dash = self.client.get("/")
        html = dash.get_data(as_text=True)
        self.assertIn("Dana", html)
        self.assertIn("Unbilled", html)
        self.client.post(f"/jobs/{job_id}/invoiced", follow_redirects=True)
        dash2 = self.client.get("/").get_data(as_text=True)
        # Should not appear in unbilled table as customer link under Unbilled section
        # Still may appear under Recently invoiced — ensure unbilled list empty of Dana via API-ish check
        with self.app.app_context():
            unbilled = H.unbilled_jobs(H.get_db())
            self.assertFalse(any(u["id"] == job_id for u in unbilled))

    def test_07_no_accept_magic_link_no_stripe(self):
        self._login()
        # routes that must not exist
        for path in ("/accept/x", "/c/token", "/s/token", "/h/token"):
            r = self.client.get(path)
            self.assertIn(r.status_code, (404, 302, 405))
        # app source sanity
        src = Path(app_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("stripe", src.lower())
        self.assertNotIn("magic", src.lower())

    def test_08_photo_skip_still_drafts(self):
        self._login()
        with self.app.app_context():
            job_id = H.create_job(
                H.get_db(),
                customer_name="Eve",
                trade_template="hvac_service",
            )
            items = H.list_job_items(H.get_db(), job_id)
        form = {}
        for item in items:
            if item["item_key"] == "trip_charge":
                form[f"selected_{item['id']}"] = "1"
        r = self.client.post(f"/jobs/{job_id}/draft", data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            job = H.get_job(H.get_db(), job_id)
            self.assertIsNone(job["photo_path"])
            lines = H.list_invoice_lines(H.get_db(), job_id)
            self.assertGreaterEqual(len(lines), 1)

    def test_09_empty_marketing_no_powered_by(self):
        self._login()
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Powered by BillCatch", html)

    def test_10_templates_loaded(self):
        tmpls = H.load_templates()
        self.assertIn("hvac_service", tmpls)
        self.assertIn("plumbing_service", tmpls)
        self.assertIn("electrical_service", tmpls)
        for key in ("hvac_service", "plumbing_service", "electrical_service"):
            items = tmpls[key]["items"]
            self.assertGreaterEqual(len(items), 5)
            kinds = {i["kind"] for i in items}
            self.assertTrue(kinds & {"flag", "hours", "qty"})


if __name__ == "__main__":
    unittest.main()
