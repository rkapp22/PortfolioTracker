"""Run the full pipeline: ingest (APIs + Excel -> staging) then transform
(staging -> dwh). This is the main manual-trigger entry point.

Run:  python src/run_pipeline.py
"""
import sys
import time
import datetime as dt

import ingest
import transform
from db import ping
from data_quality_tests import print_summary, BlockingDqError


def main() -> int:
    # Wait for the DB (compose healthcheck usually handles this, but be safe
    # when the script is run very early).
    for attempt in range(10):
        try:
            ping()
            break
        except Exception:
            print(f"[pipeline] DB not ready, retrying ({attempt + 1}/10)...", flush=True)
            time.sleep(3)
    else:
        print("[pipeline] DB never became ready. Aborting.", flush=True)
        return 1

    t0 = time.time()
    run_id = dt.datetime.now()
    all_dq = []

    try:
        print("[pipeline] === INGEST ===", flush=True)
        rc, dq = ingest.main(run_id=run_id)
        all_dq.extend(dq)
        if rc != 0:
            print_summary(all_dq)
            return rc
        print("[pipeline] === TRANSFORM ===", flush=True)
        rc, dq = transform.main(run_id=run_id)
        all_dq.extend(dq)
    except BlockingDqError as e:
        print(f"[pipeline] BLOCKED: {e}", flush=True)
        print_summary(all_dq)
        return 2

    print_summary(all_dq)
    print(f"[pipeline] Done in {time.time() - t0:.1f}s", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
