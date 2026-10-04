"""
Test runner for the job-based flow -- reads sample_jobs.json (stand-in
for the backend API), runs each job, prints results.

    python scripts/run_jobs.py
"""

import sys
import os

# app/scripts/run_jobs.py -> need the project root (parent of app/) on
# sys.path so `import app.core...` resolves regardless of cwd.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.core.job_intake import fetch_jobs
from app.core.orchestrator import TrackingOrchestrator


def main():
    jobs = fetch_jobs()
    if not jobs:
        print("No jobs found (check sample_jobs.json).")
        return

    print(f"Loaded {len(jobs)} job(s) across carriers: "
          f"{sorted(set(j.carrier for j in jobs))}\n")

    orch = TrackingOrchestrator()
    results_by_carrier = orch.track_jobs(jobs)

    for carrier, results in results_by_carrier.items():
        print(f"\n--- {carrier.upper()} ---")
        for r in results:
            print(f"  [{r.bill_to_customer_code}] {r.reference}: {r.status} "
                  f"({r.pickup_date} -> {r.delivery_date})")


if __name__ == "__main__":
    main()
