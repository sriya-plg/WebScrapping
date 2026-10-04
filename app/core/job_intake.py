"""
Where TrackingJobs come from.

The backend API isn't ready yet, so `fetch_jobs()` currently reads a
local JSON file. Once the real API exists, this is the ONLY function
that needs to change -- everything else (orchestrator, scrapers,
scheduler) just calls fetch_jobs() and doesn't care where the jobs
came from.

Expected JSON shape (a list of job objects):

[
  {
    "bill_to_customer_code": "2629916",
    "carrier": "estes",
    "url": "https://www.estes-express.com/myestes/shipment-tracking/",
    "username": "some_user",
    "password": "some_pass",
    "reference_type": "pro",
    "reference_number": "1848665720"
  },
  ...
]
"""

import json
import os

from app.core.logger import get_logger
from app.core.models import TrackingJob

logger = get_logger("job_intake")

DEFAULT_STUB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "sample_jobs.json"
)


def fetch_jobs(source_path: str = DEFAULT_STUB_PATH) -> list[TrackingJob]:
    """
    STUB IMPLEMENTATION -- reads a local JSON file.

    TODO once the backend API is ready, replace the body of this function
    with the real HTTP call, e.g.:

        import requests
        resp = requests.get(BACKEND_JOBS_URL, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        raw_jobs = resp.json()

    ...and keep everything below the fetch identical (parsing into
    TrackingJob objects, logging, error handling) -- no other file in
    this app needs to change.
    """
    if not os.path.exists(source_path):
        logger.warning("job_source_not_found", extra={"path": source_path})
        return []

    with open(source_path, "r") as f:
        raw_jobs = json.load(f)

    jobs = []
    for i, raw in enumerate(raw_jobs):
        try:
            jobs.append(TrackingJob.from_dict(raw))
        except ValueError as e:
            # Log the failure WITHOUT dumping the raw dict (it may contain
            # a password) -- just the index and the error.
            logger.error("job_parse_failed", extra={"index": i, "error": str(e)})

    logger.info("jobs_fetched", extra={"count": len(jobs), "source": source_path})
    return jobs
