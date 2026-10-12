"""Runs queued reel-saving jobs in the background, so a request returns immediately and the user can leave the app.

A small thread pool (REEL_JOB_WORKERS, default 2) bounds how many videos are downloaded and analysed at once on
this server. Jobs live in the `reel_jobs` table, so a restart loses nothing: `recover_on_startup` re-runs them.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from api import push
from pipeline.config import settings
from pipeline.orchestrator import process_reel
from pipeline.storage import job_store, supabase_store

logger = logging.getLogger("reelvault.jobs")

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=max(1, settings.reel_job_workers), thread_name_prefix="reel-job")
        return _pool


def submit(job_id: str) -> None:
    _executor().submit(run_job, job_id)


def run_job(job_id: str) -> None:
    """Process one job end to end. Never raises: every outcome is written to the job row."""
    try:
        job = job_store.claim(job_id)
        if not job:
            return  # already running, finished, or gone
        try:
            result = process_reel(job["url"], dry_run=settings.dry_run, user_id=job["user_id"], persist_json=False)
            saved = supabase_store.save_reel(result.reel)
            job_store.finish_done(job_id, saved.id)
            logger.info("Job %s done -> reel %s", job_id, saved.id)
            # Only a finished job notifies, and only once (the failure branch below never does).
            if job_store.mark_notified(job_id):
                push.notify_reel_ready(job["user_id"], job_id, saved.id, getattr(result.reel, "title", "") or "")
        except Exception as exc:
            logger.exception("Job %s failed for %s", job_id, job["url"])
            job_store.finish_failed(job_id, f"{type(exc).__name__}: {exc}")
    except Exception:
        # Storage itself is down: the row stays as it is and recover_on_startup / the next restart picks it up.
        logger.exception("Job %s could not be updated", job_id)


def recover_on_startup() -> None:
    """After a restart: jobs left mid-run (older than the stale limit, so a container that is still finishing during
    a rolling deploy isn't mistaken for dead) go back in the queue, then everything queued is submitted."""
    try:
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=settings.reel_job_stale_minutes)).isoformat()
        requeued, failed = job_store.requeue_stale(stale_before, settings.reel_job_max_attempts)
        queued = job_store.list_queued_ids()
        for job_id in queued:
            submit(job_id)
        logger.info("Job recovery: %d requeued, %d gave up, %d submitted", requeued, failed, len(queued))
    except Exception:
        logger.exception("Job recovery skipped (storage not reachable)")
