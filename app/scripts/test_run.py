"""
Manual test runner.

    python scripts/test_run.py estes 1848665720
    python scripts/test_run.py forward_air 23645286
    python scripts/test_run.py saia 77133675090

Run with SCRAPER_HEADLESS=false in .env while confirming selectors, so
you can watch the real browser and see exactly where a selector doesn't
match, before trusting a batch/headless run.
"""

import sys
import os

# app/scripts/test_run.py -> need the project root (parent of app/) on
# sys.path so `import app.core...` resolves regardless of cwd.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.core.orchestrator import TrackingOrchestrator


def main():
    if len(sys.argv) != 3:
        print("Usage: python scripts/test_run.py <carrier> <reference_number>")
        orch = TrackingOrchestrator()
        print(f"Available carriers: {orch.available_carriers()}")
        sys.exit(1)

    carrier, reference = sys.argv[1], sys.argv[2]
    orch = TrackingOrchestrator()

    result = orch.track(carrier, reference)

    print(f"\n=== {result.carrier.upper()} — {result.reference} ===")
    print(f"Status: {result.status} ({result.status_detail})")
    print(f"Pickup: {result.pickup_date}  Delivery: {result.delivery_date}")
    print(f"Shipper: {result.shipper}  ->  Consignee: {result.consignee}")
    print(f"Pieces: {result.pieces}  Weight: {result.weight_lbs}")
    print(f"\n{len(result.history)} history events:")
    for ev in result.history:
        print(f"  {ev.date}  [{ev.status}] {ev.description}")


if __name__ == "__main__":
    main()
