from fastapi.testclient import TestClient
import app as certificate_app


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(certificate_app, "DATABASE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(certificate_app, "OUTPUT_DIR", tmp_path / "certificates")
    certificate_app.initialize_database()
    return TestClient(certificate_app.app)


def payload(names=("Ada Lovelace",)):
    return {
        "title": "Introduction to Computing", "issuer": "Analytical Engines Academy",
        "issued_on": "2026-10-08",
        "recipients": [{"name": name, "email": f"recipient{i}@example.com"}
                       for i, name in enumerate(names)],
    }


def test_create_job_and_status_progress(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post("/jobs", json=payload(("Ada", "Grace")))
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress"] == {"total": 2, "processed": 2, "succeeded": 2, "failed": 0}
        result = client.get(f"/jobs/{data['id']}").json()
        assert result["progress"]["processed"] == result["progress"]["total"]
        assert all(item["status"] == "succeeded" for item in result["recipients"])


def test_input_validation(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        invalid_email = payload()
        invalid_email["recipients"][0]["email"] = "not-an-email"
        assert client.post("/jobs", json=invalid_email).status_code == 422
        empty_name = payload()
        empty_name["recipients"][0]["name"] = "  "
        assert client.post("/jobs", json=empty_name).status_code == 422
        assert client.post("/jobs", json={**payload(), "recipients": []}).status_code == 422


def test_certificate_generation_and_retrieval(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        job = client.post("/jobs", json=payload()).json()
        recipient_id = job["recipients"][0]["id"]
        response = client.get(f"/jobs/{job['id']}/certificates/{recipient_id}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")
        assert client.get(f"/jobs/{job['id']}/certificates/999999").status_code == 404


def test_individual_failure_does_not_stop_batch(tmp_path, monkeypatch):
    original = certificate_app.generate_certificate

    def fail_one(name, *args, **kwargs):
        if name == "Broken Recipient":
            raise RuntimeError("simulated render failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(certificate_app, "generate_certificate", fail_one)
    with make_client(tmp_path, monkeypatch) as client:
        job = client.post("/jobs", json=payload(("Good Recipient", "Broken Recipient"))).json()
        assert job["status"] == "completed_with_errors"
        assert job["progress"] == {"total": 2, "processed": 2, "succeeded": 1, "failed": 1}
        results = job["recipients"]
        assert [item["status"] for item in results] == ["succeeded", "failed"]
        assert "simulated render failure" in results[1]["error"]
        assert client.get(f"/jobs/{job['id']}/certificates/{results[0]['id']}").status_code == 200
        assert client.get(f"/jobs/{job['id']}/certificates/{results[1]['id']}").status_code == 409


def test_unknown_job_returns_404(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/jobs/not-a-job").status_code == 404
