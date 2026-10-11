from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.backup import next_run
from app.main import create_app
from app.store import Store


START = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
HEADERS = {"X-Requested-With": "cloud-backup"}


@pytest.fixture
def scheduled_client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.utcnow", lambda: START)
    store = Store(tmp_path)
    app = create_app(store, start_worker=False)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "test-password-only"}, headers=HEADERS)
        yield client, store, app


def save_schedule(store, name, cron="0 3 * * *", tz="UTC", enabled=True, due=None):
    store.execute("INSERT INTO schedules VALUES (?,?,?,?,?)", (name, cron, tz, enabled, due))


def test_scheduled_requires_authentication(tmp_path):
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        assert client.get("/api/scheduled").status_code == 401


def test_windows_enumerate_every_occurrence_and_sort(scheduled_client):
    client, store, app = scheduled_client
    for name in ("zeta", "alpha"):
        save_schedule(store, name, due="2026-10-12T03:00:00+00:00")
    save_schedule(store, "paused", enabled=False)
    save_schedule(store, "monthly", cron="0 3 1 * *", due="2026-11-01T03:00:00+00:00")

    short = client.get("/api/scheduled").json()
    long = client.get("/api/scheduled", params={"days": 30}).json()
    assert short["days"] == 7
    assert short["start"] == START.isoformat()
    assert short["end"] == (START + timedelta(days=7)).isoformat()
    assert len(short["jobs"]) == 14
    assert [job["stack"] for job in short["jobs"]] == ["alpha", "zeta"] * 7
    assert len(long["jobs"]) == 61
    assert "monthly" in {job["stack"] for job in long["jobs"]}
    assert "paused" not in {job["stack"] for job in long["jobs"]}
    assert short["next"] is None and long["next"] is None
    for result in (short, long):
        keys = [(job["run_at"], job["stack"]) for job in result["jobs"]]
        assert keys == sorted(keys)
        assert not any(job["overdue"] for job in result["jobs"])
    assert store.rows("SELECT * FROM jobs") == []
    assert app.state.engine is None  # Listing schedules does not require Docker.


def test_pagination_preserves_ties_and_returns_all_jobs(scheduled_client):
    client, store, _ = scheduled_client
    for name in ("alpha", "beta", "gamma"):
        save_schedule(store, name, cron="0 * * * *", due="2026-10-11T13:00:00+00:00")
    params = {"days": 7}
    actual = []
    while True:
        response = client.get("/api/scheduled", params=params)
        assert response.status_code == 200
        result = response.json()
        assert len(result["jobs"]) <= 100
        actual.extend((job["run_at"], job["stack"]) for job in result["jobs"])
        if result["next"] is None:
            break
        params.update(start=result["start"], **result["next"])
    expected = [((START + timedelta(hours=hour)).isoformat(), name)
                for hour in range(1, 169) for name in ("alpha", "beta", "gamma")]
    assert actual == expected


def test_overdue_is_shown_once_without_replaying_missed_intervals(scheduled_client):
    client, store, _ = scheduled_client
    due = "2026-10-01T03:00:00+00:00"
    save_schedule(store, "missed", due=due)
    result = client.get("/api/scheduled").json()
    assert len(result["jobs"]) == 8
    assert result["jobs"][0]["run_at"] == due
    assert result["jobs"][0]["overdue"] is True
    assert not any(job["overdue"] for job in result["jobs"][1:])
    assert result["jobs"][1]["run_at"] == "2026-10-12T03:00:00+00:00"
    assert store.rows("SELECT next_run FROM schedules")[0]["next_run"] == due


def test_pagination_across_daylight_saving(scheduled_client):
    client, store, _ = scheduled_client
    due = "2026-10-11T13:00:00+00:00"
    save_schedule(store, "hourly-london", cron="0 * * * *", tz="Europe/London", due=due)
    expected = []
    while datetime.fromisoformat(due) <= START + timedelta(days=30):
        expected.append(due)
        due = next_run("0 * * * *", "Europe/London", datetime.fromisoformat(due))
    actual, params = [], {"days": 30}
    while True:
        result = client.get("/api/scheduled", params=params).json()
        actual.extend(job["run_at"] for job in result["jobs"])
        if not result["next"]:
            break
        params.update(start=result["start"], **result["next"])
    assert actual == expected


def test_timezone_and_daylight_saving_use_scheduler_times(scheduled_client):
    client, store, _ = scheduled_client
    save_schedule(store, "london", tz="Europe/London", due="2026-10-12T02:00:00+00:00")
    jobs = client.get("/api/scheduled", params={"days": 30}).json()["jobs"]
    assert jobs[0]["timezone"] == "Europe/London"
    assert jobs[12]["run_at"] == "2026-10-24T02:00:00+00:00"
    # Use the same transition behavior as the scheduler's persisted due times.
    assert jobs[13]["run_at"] == next_run("0 3 * * *", "Europe/London",
                                             datetime.fromisoformat(jobs[12]["run_at"]))
    assert jobs[14]["run_at"] == "2026-10-26T03:00:00+00:00"


def test_empty_and_missing_due_time(scheduled_client):
    client, store, _ = scheduled_client
    assert client.get("/api/scheduled").json()["jobs"] == []
    save_schedule(store, "without-due")
    jobs = client.get("/api/scheduled").json()["jobs"]
    assert len(jobs) == 7
    assert jobs[0]["run_at"] == "2026-10-12T03:00:00+00:00"


@pytest.mark.parametrize("params", [
    {"days": 8}, {"days": 0}, {"days": 31}, {"days": "invalid"},
    {"start": "2026-10-11T12:00:00"}, {"after": "2026-10-11T13:00:00"},
    {"start": "invalid"},
])
def test_invalid_windows_and_dates_are_rejected(scheduled_client, params):
    client, _, _ = scheduled_client
    assert client.get("/api/scheduled", params=params).status_code == 422
