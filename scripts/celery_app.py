"""
celery_app.py
=============
Celery worker that wraps generate_video.py as a task, triggered by n8n webhooks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog
from celery import Celery
from celery.utils.log import get_task_logger

from scripts.generate_video import VideoPayload, post_webhook, render_platform

BROKER = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

app = Celery("go_viral", broker=BROKER, backend=BACKEND)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_routes={"scripts.celery_app.render_video": {"queue": "video"}},
    task_track_started=True,
    worker_prefetch_multiplier=1,
)

log = get_task_logger(__name__)


@app.task(bind=True, max_retries=2, default_retry_delay=30)
def render_video(self, payload_dict: dict) -> dict:
    """
    Celery task: validate payload, render all platforms, post webhook.

    payload_dict keys match VideoPayload fields.
    """
    task_id = self.request.id
    log.info(f"[{task_id}] render_video started")

    try:
        payload = VideoPayload(**payload_dict)
        payload.validate()
    except (ValueError, FileNotFoundError) as exc:
        log.error(f"[{task_id}] payload invalid: {exc}")
        raise ValueError(str(exc)) from exc

    results: dict[str, str] = {}
    errors: dict[str, str] = {}

    for platform in payload.platforms:
        try:
            out = render_platform(payload, platform)
            results[platform] = out
            self.update_state(
                state="PROGRESS",
                meta={"completed": list(results.keys()), "pending": [p for p in payload.platforms if p not in results]},
            )
        except Exception as exc:
            log.error(f"[{task_id}] platform {platform} failed: {exc}")
            errors[platform] = str(exc)
            try:
                raise self.retry(exc=exc)
            except self.MaxRetriesExceededError:
                pass

    summary = {
        "task_id": task_id,
        "status": "error" if errors and not results else ("partial" if errors else "ok"),
        "outputs": results,
        "errors": errors,
    }

    if payload.webhook_url:
        post_webhook(payload.webhook_url, summary)

    log.info(f"[{task_id}] done: {summary['status']}")
    return summary
