from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.research.service import ResearchService
from app.research.store import ResearchStore


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("friday.research")


async def run_worker() -> None:
    settings = get_settings()
    store = ResearchStore(settings.data_dir / "research.sqlite3", recover_interrupted=True)
    service = ResearchService(settings, store)
    logger.info("FRIDAY research worker ready with %s", settings.research_model)
    while True:
        job = store.claim_next()
        if job is None:
            await asyncio.sleep(settings.research_poll_seconds)
            continue
        logger.info("Research started: %s (%s)", job["topic"], job["tier"])
        result = await service.run_job(job)
        logger.info("Research %s: %s", result["status"], result["job_id"])


if __name__ == "__main__":
    asyncio.run(run_worker())
