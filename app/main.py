"""Application entry point for processing the configured tracking jobs.

Run from the repository root with::

    python -m app.main
"""

from app.core.job_intake import fetch_jobs
from app.core.orchestrator import TrackingOrchestrator


def main() -> None:
    """Fetch pending jobs, track them, and print a concise summary."""
    jobs = fetch_jobs()
    if not jobs:
        print("No jobs found (check sample_jobs.json).")
        return

    print(
        f"Loaded {len(jobs)} job(s) across carriers: "
        f"{sorted({job.carrier for job in jobs})}\n"
    )

    results_by_carrier = TrackingOrchestrator().track_jobs(jobs)
    for carrier, results in results_by_carrier.items():
        print(f"\n--- {carrier.upper()} ---")
        for result in results:
            print(
                f"  [{result.bill_to_customer_code}] {result.reference}: "
                f"{result.status} ({result.pickup_date} -> {result.delivery_date})"
            )


if __name__ == "__main__":
    main()
