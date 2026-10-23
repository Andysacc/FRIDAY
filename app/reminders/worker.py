import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings, get_settings
from app.state.task_store import TaskStore


logger = logging.getLogger("friday.reminders")
DeliveryHandler = Callable[[dict[str, Any]], Awaitable[None]]


class UnsupportedReminderChannel(RuntimeError):
    pass


async def deliver_to_dashboard(delivery: dict[str, Any]) -> None:
    """Dashboard clients retrieve durable sent records through the API."""


class ReminderWorker:
    def __init__(
        self,
        store: TaskStore,
        settings: Settings,
        *,
        handlers: dict[str, DeliveryHandler] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.handlers = handlers or {"dashboard": deliver_to_dashboard}
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> int:
        current = self.now().astimezone(timezone.utc)
        stale_before = current - timedelta(seconds=self.settings.reminder_claim_timeout_seconds)
        recovered = self.store.recover_stale_reminder_deliveries(
            stale_before=stale_before.isoformat(),
            retry_at=current.isoformat(),
            max_attempts=self.settings.reminder_max_attempts,
        )
        if recovered:
            logger.warning("recovered %s interrupted reminder deliveries", recovered)

        deliveries = self.store.claim_due_reminders(
            now=current.isoformat(),
            limit=self.settings.reminder_batch_size,
        )
        for delivery in deliveries:
            await self._dispatch(delivery)
        return len(deliveries)

    async def _dispatch(self, delivery: dict[str, Any]) -> None:
        channel = str(delivery["channel"])
        handler = self.handlers.get(channel)
        if handler is None:
            await self._finish_failure(
                delivery,
                UnsupportedReminderChannel(f"{channel}_channel_not_configured"),
                retryable=False,
            )
            return

        try:
            await handler(delivery)
        except UnsupportedReminderChannel as exc:
            await self._finish_failure(delivery, exc, retryable=False)
        except Exception as exc:  # noqa: BLE001 - delivery drivers are an external boundary
            logger.exception("reminder delivery failed", extra={"delivery_id": delivery["delivery_id"]})
            await self._finish_failure(delivery, exc, retryable=True)
        else:
            finished = self.now().astimezone(timezone.utc).isoformat()
            self.store.finish_reminder_delivery(
                delivery["delivery_id"],
                succeeded=True,
                finished_at=finished,
            )
            logger.info(
                "reminder delivered",
                extra={
                    "delivery_id": delivery["delivery_id"],
                    "reminder_id": delivery["reminder_id"],
                    "channel": channel,
                },
            )

    async def _finish_failure(
        self,
        delivery: dict[str, Any],
        error: Exception,
        *,
        retryable: bool,
    ) -> None:
        finished = self.now().astimezone(timezone.utc)
        can_retry = retryable and int(delivery["attempt"]) < self.settings.reminder_max_attempts
        retry_at = (
            finished + timedelta(
                seconds=self.settings.reminder_retry_seconds * (2 ** (int(delivery["attempt"]) - 1))
            )
            if can_retry
            else None
        )
        self.store.finish_reminder_delivery(
            delivery["delivery_id"],
            succeeded=False,
            finished_at=finished.isoformat(),
            error=str(error)[:1000],
            retry_at=retry_at.isoformat() if retry_at is not None else None,
        )

    async def run_forever(self) -> None:
        logger.info("reminder worker started")
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - keep the durable worker alive after one bad cycle
                logger.exception("reminder worker cycle failed")
            await asyncio.sleep(self.settings.reminder_poll_seconds)


async def main() -> None:
    settings = get_settings()
    store = TaskStore(settings.data_dir / "orchestrator.sqlite3")
    worker = ReminderWorker(store, settings)
    await worker.run_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
