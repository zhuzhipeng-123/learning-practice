"""Small, privacy-safe diagnostics for local model jobs."""

import json
import logging
from time import monotonic

LOGGER = logging.getLogger("learning.model_job")


def started_at():
    return monotonic()


def log_event(job_id, module, execution_id, stage, *, started=None, reply=None, error=None):
    event = {
        "job_id": job_id,
        "module": module,
        "execution_id": execution_id,
        "stage": stage,
    }
    if started is not None:
        event["elapsed_ms"] = round((monotonic() - started) * 1000)
    if reply is not None:
        event["model"] = getattr(reply, "model", None)
        usage = getattr(reply, "usage", None)
        if isinstance(usage, dict):
            event["usage"] = {key: value for key, value in usage.items()
                              if isinstance(key, str) and isinstance(value, (int, float))}
    if error is not None:
        event["error_type"] = type(error).__name__
    LOGGER.info("model_job %s", json.dumps(event, ensure_ascii=True, sort_keys=True))
