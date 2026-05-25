"""Run the full pipeline: ingest (APIs + Excel -> staging) then transform
(staging -> dwh). This is the main manual-trigger entry point.

Run:  python src/run_pipeline.py
"""
import sys
import time

import ingest
import transform
from db import ping


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
    print("[pipeline] === INGEST ===", flush=True)
    rc = ingest.main()
    if rc != 0:
        return rc
    print("[pipeline] === TRANSFORM ===", flush=True)
    rc = transform.main()
    print(f"[pipeline] Done in {time.time() - t0:.1f}s", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
