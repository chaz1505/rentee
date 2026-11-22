"""Render background worker for durable external iProperty lookup jobs."""

import os
import time

from external_scraper import (
    claim_external_job,
    external_scraper_enabled,
    process_external_job,
)


def main():
    interval = max(2, int(os.getenv("EXTERNAL_SCRAPER_WORKER_POLL_SECONDS", "10")))
    while True:
        if not external_scraper_enabled():
            time.sleep(interval)
            continue
        try:
            job = claim_external_job("live")
            if job:
                process_external_job(job, "live")
            else:
                time.sleep(interval)
        except Exception as error:
            print(f"[EXTERNAL SCRAPER WORKER] error={type(error).__name__}", flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    main()
