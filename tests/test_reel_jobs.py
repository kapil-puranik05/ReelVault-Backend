"""Background reel saving: a shared link is queued and answered at once; the pipeline runs separately, so the app
can close. Supabase and the AI pipeline are replaced with in-memory fakes; routes, guard and job logic are real."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api import push, reel_jobs
from pipeline.config import settings
from pipeline.storage import job_store, supabase_store
from tests.test_subscription import OTHER, USER, env  # noqa: F401  (env is a fixture)

URL = "https://www.instagram.com/reel/Cx12AbC/"


class FakeJobs:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.reels: dict[str, str] = {}  # url -> reel id, per user "USER|url"
        self._ids = (f"00000000-0000-0000-0000-{n:012d}" for n in itertools.count(1))
        self.submitted: list[str] = []
        self.pushes: list[tuple] = []
        self._notified: set[str] = set()

    def now(self, delta=0):
        return (datetime.now(timezone.utc) + timedelta(minutes=delta)).isoformat()

    def install(self, monkeypatch):
        db = self

        def create(user_id, url):
            if any(j["user_id"] == user_id and j["url"] == url and j["status"] in ("QUEUED", "PROCESSING")
                   for j in db.jobs.values()):
                raise RuntimeError("duplicate key value violates unique constraint (23505)")
            job = {"id": next(db._ids), "user_id": user_id, "url": url, "status": "QUEUED", "reel_id": None,
                   "error": None, "attempts": 0, "created_at": db.now(), "updated_at": db.now()}
            db.jobs[job["id"]] = job
            return dict(job)

        def active_for(user_id):
            return [j for j in db.jobs.values() if j["user_id"] == user_id and j["status"] in ("QUEUED", "PROCESSING")]

        def claim(job_id):
            job = db.jobs.get(job_id)
            if not job or job["status"] != "QUEUED":
                return None
            job.update(status="PROCESSING", attempts=job["attempts"] + 1)
            return dict(job)

        def finish(job_id, **fields):
            if db.jobs[job_id]["status"] == "PROCESSING":
                db.jobs[job_id].update(fields)

        def requeue_stale(stale_before_iso, max_attempts):
            requeued = failed = 0
            for j in db.jobs.values():
                if j["status"] == "PROCESSING" and j["updated_at"] < stale_before_iso:
                    if j["attempts"] >= max_attempts:
                        j.update(status="FAILED", error="Gave up after repeated interruptions"); failed += 1
                    else:
                        j["status"] = "QUEUED"; requeued += 1
            return requeued, failed

        def delete_finished(user_id, job_id):
            j = db.jobs.get(job_id)
            if j and j["user_id"] == user_id and j["status"] in ("DONE", "FAILED"):
                del db.jobs[job_id]
                return True
            return False

        monkeypatch.setattr(job_store, "create", create)
        monkeypatch.setattr(job_store, "get", lambda jid: dict(db.jobs[jid]) if jid in db.jobs else None)
        monkeypatch.setattr(job_store, "find_active_by_url",
                            lambda u, url: next((dict(j) for j in active_for(u) if j["url"] == url), None))
        monkeypatch.setattr(job_store, "count_active", lambda u: len(active_for(u)))
        monkeypatch.setattr(job_store, "list_recent",
                            lambda u, since: [dict(j) for j in db.jobs.values() if j["user_id"] == u])
        def mark_notified(job_id):
            if db.jobs[job_id]["status"] != "DONE" or job_id in db._notified:
                return False
            db._notified.add(job_id)
            return True

        monkeypatch.setattr(job_store, "mark_notified", mark_notified)
        monkeypatch.setattr(push, "notify_reel_ready",
                            lambda uid, jid, rid, title="": db.pushes.append((uid, jid, rid, title)) or 1)
        monkeypatch.setattr(job_store, "claim", claim)
        monkeypatch.setattr(job_store, "finish_done",
                            lambda jid, rid: finish(jid, status="DONE", reel_id=rid, error=None))
        monkeypatch.setattr(job_store, "finish_failed", lambda jid, err: finish(jid, status="FAILED", error=err))
        monkeypatch.setattr(job_store, "delete_finished", delete_finished)
        monkeypatch.setattr(job_store, "requeue_stale", requeue_stale)
        monkeypatch.setattr(job_store, "list_queued_ids",
                            lambda: [j["id"] for j in db.jobs.values() if j["status"] == "QUEUED"])
        monkeypatch.setattr(supabase_store, "find_reel_id_by_url", lambda u, url: db.reels.get(f"{u}|{url}"))
        # The request must NOT run the pipeline: capture submissions, run them only when the test says so.
        monkeypatch.setattr(reel_jobs, "submit", lambda jid: db.submitted.append(jid))


@pytest.fixture
def jobs(env, monkeypatch):  # noqa: F811
    db, _, client = env
    db.set_trial(USER, days_from_now=3)
    fake = FakeJobs()
    fake.install(monkeypatch)
    monkeypatch.setattr(settings, "reel_job_max_active_per_user", 3)
    return fake, client


def fake_pipeline(monkeypatch, *, fail=None, reel_id="reel-1"):
    calls = []

    def process(url, **kw):
        calls.append((url, kw["user_id"]))
        if fail:
            raise RuntimeError(fail)
        return SimpleNamespace(reel=SimpleNamespace(id=reel_id, title="Garlic pasta in 10 minutes"))

    monkeypatch.setattr(reel_jobs, "process_reel", process)
    monkeypatch.setattr(supabase_store, "save_reel", lambda record: SimpleNamespace(id=record.id))
    return calls


# ── The request returns immediately; the work happens separately ─────────────

def test_queue_answers_at_once_without_running_the_pipeline(jobs, monkeypatch):
    fake, client = jobs
    calls = fake_pipeline(monkeypatch)
    res = client.post("/api/reels/queue", json={"url": URL})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "QUEUED" and body["url"] == URL
    assert calls == [] and fake.submitted == [body["id"]]  # handed to the background, not run in the request


def test_background_job_saves_the_reel_without_the_app(jobs, monkeypatch):
    fake, client = jobs
    calls = fake_pipeline(monkeypatch)
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    # ... the app is gone now; the server runs the job by itself:
    reel_jobs.run_job(job_id)
    assert calls == [(URL, USER)]
    assert fake.jobs[job_id]["status"] == "DONE" and fake.jobs[job_id]["reel_id"] == "reel-1"
    listed = client.get("/api/reels/jobs").json()["jobs"]
    assert [(j["id"], j["status"], j["reelId"]) for j in listed] == [(job_id, "DONE", "reel-1")]


def test_failed_job_is_reported_with_a_plain_message_and_no_internals(jobs, monkeypatch):
    fake, client = jobs
    fake_pipeline(monkeypatch, fail="yt-dlp: login required key=sk-secret")
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    reel_jobs.run_job(job_id)
    job = client.get("/api/reels/jobs").json()["jobs"][0]
    assert job["status"] == "FAILED" and "private or unsupported" in job["message"]
    assert "sk-secret" not in str(job) and "yt-dlp" not in str(job)
    assert "yt-dlp" in fake.jobs[job_id]["error"]  # kept for us, in the table


def test_a_job_runs_only_once_even_if_picked_up_twice(jobs, monkeypatch):
    fake, client = jobs
    calls = fake_pipeline(monkeypatch)
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    reel_jobs.run_job(job_id)
    reel_jobs.run_job(job_id)
    assert len(calls) == 1


# ── Duplicates and limits ────────────────────────────────────────────────────

def test_sharing_the_same_link_twice_does_not_create_two_jobs(jobs):
    fake, client = jobs
    first = client.post("/api/reels/queue", json={"url": URL}).json()
    second = client.post("/api/reels/queue", json={"url": URL}).json()
    assert first["id"] == second["id"] and len(fake.jobs) == 1 and len(fake.submitted) == 1


def test_lost_insert_race_returns_the_winning_job(jobs, monkeypatch):
    fake, client = jobs
    winner = job_store.create(USER, URL)  # the other request got there first
    seen = {"n": 0}

    def find(u, url):  # our first look finds nothing (before their insert landed); afterwards the winner is visible
        seen["n"] += 1
        return None if seen["n"] == 1 else dict(winner)

    monkeypatch.setattr(job_store, "find_active_by_url", find)
    res = client.post("/api/reels/queue", json={"url": URL})
    assert res.status_code == 202 and res.json()["id"] == winner["id"] and len(fake.jobs) == 1


def test_already_saved_link_is_not_processed_again(jobs):
    fake, client = jobs
    fake.reels[f"{USER}|{URL}"] = "existing-reel"
    res = client.post("/api/reels/queue", json={"url": URL})
    assert res.status_code == 200 and res.json() == {"status": "ALREADY_SAVED", "reelId": "existing-reel"}
    assert fake.jobs == {} and fake.submitted == []


def test_too_many_active_jobs_are_refused(jobs):
    fake, client = jobs
    for n in range(3):
        assert client.post("/api/reels/queue", json={"url": f"{URL}?n={n}"}).status_code == 202
    res = client.post("/api/reels/queue", json={"url": f"{URL}?n=99"})
    assert res.status_code == 429 and "error" in res.json()


# ── Access and isolation ─────────────────────────────────────────────────────

def test_expired_trial_cannot_queue(env, monkeypatch):  # noqa: F811
    db, _, client = env
    db.set_trial(USER, days_from_now=-1)
    FakeJobs().install(monkeypatch)
    res = client.post("/api/reels/queue", json={"url": URL})
    assert res.status_code == 402 and res.json()["code"] == "SUBSCRIPTION_REQUIRED"


def test_invalid_url_is_rejected(jobs):
    _, client = jobs
    assert client.post("/api/reels/queue", json={"url": "not a url"}).status_code == 400


def test_users_only_see_and_dismiss_their_own_jobs(jobs, monkeypatch):
    fake, client = jobs
    mine = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    theirs = job_store.create(OTHER, "https://youtube.com/shorts/zzz")
    fake.jobs[theirs["id"]]["status"] = "FAILED"
    fake.jobs[mine]["status"] = "FAILED"
    assert [j["id"] for j in client.get("/api/reels/jobs").json()["jobs"]] == [mine]
    assert client.delete(f"/api/reels/jobs/{theirs['id']}").status_code == 404 and theirs["id"] in fake.jobs
    assert client.delete(f"/api/reels/jobs/{mine}").status_code == 200 and mine not in fake.jobs


def test_a_running_job_cannot_be_dismissed(jobs):
    fake, client = jobs
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    assert client.delete(f"/api/reels/jobs/{job_id}").status_code == 404 and job_id in fake.jobs


# ── Restart recovery ─────────────────────────────────────────────────────────

def test_restart_requeues_stuck_jobs_and_runs_everything_queued(jobs, monkeypatch):
    fake, client = jobs
    calls = fake_pipeline(monkeypatch)
    stuck = job_store.create(USER, URL + "a"); fake.jobs[stuck["id"]].update(status="PROCESSING", attempts=1, updated_at=fake.now(-60))
    fresh = job_store.create(USER, URL + "b"); fake.jobs[fresh["id"]].update(status="PROCESSING", attempts=1, updated_at=fake.now(0))
    tired = job_store.create(USER, URL + "c"); fake.jobs[tired["id"]].update(status="PROCESSING", attempts=3, updated_at=fake.now(-60))
    waiting = job_store.create(USER, URL + "d")
    reel_jobs.recover_on_startup()
    assert fake.jobs[stuck["id"]]["status"] == "QUEUED"      # presumed dead: retried
    assert fake.jobs[fresh["id"]]["status"] == "PROCESSING"  # another container may still be on it: left alone
    assert fake.jobs[tired["id"]]["status"] == "FAILED"      # tried enough times
    assert set(fake.submitted) == {stuck["id"], waiting["id"]}
    for jid in fake.submitted:
        reel_jobs.run_job(jid)
    assert sorted(c[0] for c in calls) == [URL + "a", URL + "d"]


def test_recovery_never_crashes_startup_when_storage_is_down(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(job_store, "requeue_stale", boom)
    reel_jobs.recover_on_startup()  # must not raise


# ── The real thread pool: the request must not wait for the pipeline ───────────

def test_request_returns_while_the_pipeline_is_still_running(env, monkeypatch):  # noqa: F811
    import threading
    import time

    db, _, client = env
    db.set_trial(USER, days_from_now=3)
    real_submit = reel_jobs.submit
    fake = FakeJobs()
    fake.install(monkeypatch)
    monkeypatch.setattr(reel_jobs, "submit", real_submit)  # this test wants the real thread pool
    monkeypatch.setattr(reel_jobs, "_pool", None)

    started, release = threading.Event(), threading.Event()

    def slow_pipeline(url, **kw):
        started.set()
        assert release.wait(timeout=5)  # the "AI work" takes as long as the test lets it
        return SimpleNamespace(reel=SimpleNamespace(id="reel-slow"))

    monkeypatch.setattr(reel_jobs, "process_reel", slow_pipeline)
    monkeypatch.setattr(supabase_store, "save_reel", lambda record: SimpleNamespace(id=record.id))

    res = client.post("/api/reels/queue", json={"url": URL})  # returns although the pipeline has not finished
    job_id = res.json()["id"]
    assert res.status_code == 202 and started.wait(timeout=5)
    assert fake.jobs[job_id]["status"] == "PROCESSING"  # running on a server thread, request already answered

    release.set()
    for _ in range(100):
        if fake.jobs[job_id]["status"] == "DONE":
            break
        time.sleep(0.05)
    assert fake.jobs[job_id]["status"] == "DONE" and fake.jobs[job_id]["reel_id"] == "reel-slow"
    reel_jobs._pool.shutdown(wait=True)


# ── "Your reel is ready" notification: only for a finished job, and only once ──

def test_finished_job_sends_one_ready_notification(jobs, monkeypatch):
    fake, client = jobs
    fake_pipeline(monkeypatch)
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    assert fake.pushes == []  # nothing yet: the upload being accepted is not completion
    reel_jobs.run_job(job_id)
    assert fake.pushes == [(USER, job_id, "reel-1", "Garlic pasta in 10 minutes")]


def test_a_failed_job_never_sends_a_success_notification(jobs, monkeypatch):
    fake, client = jobs
    fake_pipeline(monkeypatch, fail="boom")
    reel_jobs.run_job(client.post("/api/reels/queue", json={"url": URL}).json()["id"])
    assert fake.pushes == []


def test_duplicate_completion_events_notify_once(jobs, monkeypatch):
    fake, client = jobs
    fake_pipeline(monkeypatch)
    job_id = client.post("/api/reels/queue", json={"url": URL}).json()["id"]
    reel_jobs.run_job(job_id)
    reel_jobs.run_job(job_id)                        # picked up twice
    fake.jobs[job_id]["status"] = "DONE"
    assert job_store.mark_notified(job_id) is False  # and the one-shot guard stays closed
    assert len(fake.pushes) == 1
