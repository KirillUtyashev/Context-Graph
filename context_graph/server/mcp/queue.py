"""Queue helpers for backend-native MCP write scaffolds."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

OBSERVATION_WRITE_CAPABILITY = "context_graph.backend.ingestion.observation_write"
OBSERVATION_WRITE_TODO = "TODO(context_graph.backend.ingestion.observation_write)"


@dataclass(frozen=True, slots=True)
class ObservationWriteRequest:
    """One scoped agent observation write request."""

    repo_id: str
    group_id: str
    observation_text: str
    observation_kind: str
    task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObservationWriteResult:
    """Result envelope for queued observation-write requests."""

    repo_id: str
    group_id: str
    accepted: bool
    persisted: bool
    observation_id: str | None
    queue_position: int
    message: str
    capability: str = OBSERVATION_WRITE_CAPABILITY
    todo: str = OBSERVATION_WRITE_TODO


class ObservationWriteService:
    """Scaffolded write-back boundary for agent observations."""

    def preview_result(
        self,
        request: ObservationWriteRequest,
        *,
        queue_position: int,
    ) -> ObservationWriteResult:
        return ObservationWriteResult(
            repo_id=request.repo_id,
            group_id=request.group_id,
            accepted=True,
            persisted=False,
            observation_id=None,
            queue_position=queue_position,
            message=(
                "record_agent_observation is scaffolded; the request was accepted "
                "but not persisted."
            ),
        )

    async def process_request(self, request: ObservationWriteRequest) -> None:
        """Process one queued request.

        The backend-native mutation path is intentionally deferred in this pass.
        """

        logger.info(
            "Skipping persistence for scaffolded observation write repo_id=%s group_id=%s task_id=%s",
            request.repo_id,
            request.group_id,
            request.task_id,
        )


class QueueService:
    """Serialize background work by queue id."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[Callable[[], Awaitable[None]]]] = {}
        self._workers: dict[str, bool] = {}

    async def add_task(
        self,
        queue_id: str,
        process_func: Callable[[], Awaitable[None]],
    ) -> int:
        """Queue one async task and return its queue position."""

        if queue_id not in self._queues:
            self._queues[queue_id] = asyncio.Queue()
        await self._queues[queue_id].put(process_func)
        if not self._workers.get(queue_id, False):
            asyncio.create_task(self._process_queue(queue_id))
        return self._queues[queue_id].qsize()

    async def _process_queue(self, queue_id: str) -> None:
        logger.info("Starting MCP queue worker for queue_id=%s", queue_id)
        self._workers[queue_id] = True
        try:
            while True:
                process_func = await self._queues[queue_id].get()
                try:
                    await process_func()
                except Exception as exc:
                    logger.error("Error processing queued MCP task for %s: %s", queue_id, exc)
                finally:
                    self._queues[queue_id].task_done()
        except asyncio.CancelledError:
            logger.info("MCP queue worker for queue_id=%s was cancelled", queue_id)
        except Exception as exc:
            logger.error("Unexpected MCP queue worker error for %s: %s", queue_id, exc)
        finally:
            self._workers[queue_id] = False
            logger.info("Stopped MCP queue worker for queue_id=%s", queue_id)

    def get_queue_size(self, queue_id: str) -> int:
        if queue_id not in self._queues:
            return 0
        return self._queues[queue_id].qsize()

    def is_worker_running(self, queue_id: str) -> bool:
        return self._workers.get(queue_id, False)

    async def enqueue_observation_write(
        self,
        request: ObservationWriteRequest,
        *,
        write_service: ObservationWriteService,
    ) -> ObservationWriteResult:
        """Queue one scaffolded observation-write request."""

        queue_id = f"{request.repo_id}:{request.group_id}"

        async def process_request() -> None:
            await write_service.process_request(request)

        queue_position = await self.add_task(queue_id, process_request)
        return write_service.preview_result(request, queue_position=queue_position)
