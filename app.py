"""Bulk certificate generation API backed by SQLite and generated PDFs."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("CERTIFICATE_DATABASE", BASE_DIR / "certificates.db"))
OUTPUT_DIR = Path(os.getenv("CERTIFICATE_OUTPUT_DIR", BASE_DIR / "generated"))
MAX_RECIPIENTS = 5000
app = FastAPI(title="Bulk Certificate Generator", version="1.0.0")
WEB_DIR = BASE_DIR / "web"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, issuer TEXT NOT NULL,
                issued_on TEXT NOT NULL, status TEXT NOT NULL,
                total INTEGER NOT NULL, succeeded INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL
                    REFERENCES jobs(id) ON DELETE CASCADE,
                name TEXT NOT NULL, email TEXT NOT NULL, status TEXT NOT NULL,
                certificate_path TEXT, error TEXT
            );
        """)
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """Serve the non-technical batch generation page."""
    return FileResponse(WEB_DIR / "index.html")


class RecipientInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    email: EmailStr


class JobInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=160)
    issued_on: date
    recipients: list[RecipientInput] = Field(min_length=1, max_length=MAX_RECIPIENTS)


def generate_certificate(recipient: str, title: str, issuer: str,
                         issued_on: str, destination: Path) -> None:
    """Render one certificate so failures can be isolated per recipient."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(destination), pagesize=landscape(letter))
    width, height = landscape(letter)
    pdf.setStrokeColorRGB(0.70, 0.55, 0.25)
    pdf.setLineWidth(4)
    pdf.rect(0.35 * inch, 0.35 * inch, width - 0.7 * inch, height - 0.7 * inch)
    pdf.setFillColorRGB(0.12, 0.18, 0.28)
    pdf.setFont("Helvetica-Bold", 30)
    pdf.drawCentredString(width / 2, height * 0.73, "CERTIFICATE OF COMPLETION")
    pdf.setFillColorRGB(0.25, 0.25, 0.25)
    pdf.setFont("Helvetica", 16)
    pdf.drawCentredString(width / 2, height * 0.60, "This certificate is proudly presented to")
    pdf.setFillColorRGB(0.12, 0.18, 0.28)
    pdf.setFont("Helvetica-Bold", 28)
    pdf.drawCentredString(width / 2, height * 0.49, recipient)
    pdf.setFillColorRGB(0.25, 0.25, 0.25)
    pdf.setFont("Helvetica", 16)
    pdf.drawCentredString(width / 2, height * 0.38, f"for completing {title}")
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(width / 2, height * 0.23, f"Issued {issued_on} by {issuer}")
    pdf.save()


def process_job(job_id: str) -> None:
    with closing(connect()) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        recipients = conn.execute(
            "SELECT * FROM recipients WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    succeeded = failed = 0
    for item in recipients:
        destination = OUTPUT_DIR / job_id / f"{item['id']}.pdf"
        try:
            generate_certificate(item["name"], job["title"], job["issuer"],
                                 job["issued_on"], destination)
            with closing(connect()) as conn:
                conn.execute("UPDATE recipients SET status='succeeded', certificate_path=?, error=NULL WHERE id=?",
                             (str(destination), item["id"]))
                conn.execute("UPDATE jobs SET succeeded=succeeded+1 WHERE id=?", (job_id,))
                conn.commit()
            succeeded += 1
        except Exception as exc:
            with closing(connect()) as conn:
                conn.execute("UPDATE recipients SET status='failed', error=? WHERE id=?",
                             (str(exc)[:500], item["id"]))
                conn.execute("UPDATE jobs SET failed=failed+1 WHERE id=?", (job_id,))
                conn.commit()
            failed += 1
    status = "completed" if not failed else "completed_with_errors"
    with closing(connect()) as conn:
        conn.execute("UPDATE jobs SET status=?, succeeded=?, failed=?, completed_at=? WHERE id=?",
                     (status, succeeded, failed, datetime.now(timezone.utc).isoformat(), job_id))
        conn.commit()


def job_response(job_id: str) -> dict:
    with closing(connect()) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        rows = conn.execute("SELECT id, name, email, status, error FROM recipients WHERE job_id=? ORDER BY id",
                            (job_id,)).fetchall()
    return {
        "id": job["id"], "title": job["title"], "issuer": job["issuer"],
        "issued_on": job["issued_on"], "status": job["status"],
        "progress": {"total": job["total"], "processed": job["succeeded"] + job["failed"],
                     "succeeded": job["succeeded"], "failed": job["failed"]},
        "created_at": job["created_at"], "completed_at": job["completed_at"],
        "recipients": [dict(row) for row in rows],
    }


@app.post("/jobs", status_code=202)
def create_job(payload: JobInput, background_tasks: BackgroundTasks) -> dict:
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO jobs(id,title,issuer,issued_on,status,total,created_at) VALUES(?,?,?,?,?,?,?)",
                     (job_id, payload.title, payload.issuer, payload.issued_on.isoformat(),
                      "queued", len(payload.recipients), now))
        conn.executemany("INSERT INTO recipients(job_id,name,email,status) VALUES(?,?,?,'pending')",
                         [(job_id, item.name, str(item.email)) for item in payload.recipients])
        conn.commit()
    with closing(connect()) as conn:
        conn.execute("UPDATE jobs SET status='processing' WHERE id=?", (job_id,))
        conn.commit()
    background_tasks.add_task(process_job, job_id)
    return job_response(job_id)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return job_response(job_id)


@app.get("/jobs/{job_id}/certificates/{recipient_id}")
def get_certificate(job_id: str, recipient_id: int) -> Response:
    with closing(connect()) as conn:
        row = conn.execute("SELECT status, certificate_path FROM recipients WHERE id=? AND job_id=?",
                            (recipient_id, job_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if row["status"] != "succeeded" or not row["certificate_path"]:
        raise HTTPException(status_code=409, detail="Certificate is not available")
    try:
        content = Path(row["certificate_path"]).read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Certificate file not found") from exc
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="certificate-{recipient_id}.pdf"'
    })
