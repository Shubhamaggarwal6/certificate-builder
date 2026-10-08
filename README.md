# Bulk Certificate Generator

FastAPI API that stores bulk jobs and per-recipient results in SQLite, renders a built-in PDF certificate for each valid recipient, and exposes status and download endpoints.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

SQLite defaults to `certificates.db` beside the app; PDFs default to `generated/`. Override with `CERTIFICATE_DATABASE` and `CERTIFICATE_OUTPUT_DIR`.

## API

Open `http://127.0.0.1:8000` after starting the server to use the form. Enter the course/event name, issuing organization, and issue date. Choose whether the certificates are for one person or multiple people, then enter names and email addresses in the table. Select **Create certificates** to issue them together. The page handles the job tracking automatically, shows progress, and provides a separate PDF download for every successful recipient. You do not need to enter a job ID or use the technical API documentation.

For integrations, `POST /jobs` accepts 1–5,000 recipients and returns HTTP 202 with a job ID. Example body:

```json
{
  "title": "Introduction to Computing",
  "issuer": "Example Academy",
  "issued_on": "2026-10-08",
  "recipients": [{"name": "Ada Lovelace", "email": "ada@example.com"}]
}
```

`GET /jobs/{job_id}` provides status, progress counts, and recipient-level outcomes/errors. `GET /jobs/{job_id}/certificates/{recipient_id}` downloads a PDF; it returns 409 while unavailable and 404 for an unknown record or missing file. Invalid batch input returns 422 without creating a job.

## Processing choice

FastAPI `BackgroundTasks` returns the job ID without holding the request open while the PDFs are rendered. State is persisted in SQLite and each recipient is processed independently, so an individual rendering error is recorded and does not stop the rest of the batch. Job status becomes `completed` or `completed_with_errors`. This in-process queue fits a single application process and moderate batches. For high volume or multi-worker deployment, move `process_job` to a durable worker queue such as Celery or RQ.

## Tests

After installing requirements, run `pytest`. Tests cover job creation/progress, validation, certificate generation/retrieval, recipient-level failure isolation, and unknown jobs.
