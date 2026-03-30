"""Timeline assembly for provenance graph records."""

from __future__ import annotations

from context_graph.backend import BackendDriver

from ..schemas.provenance import TimelineResponse
from .models import IMPLEMENTED_STATUS
from .shared import _expand_backend_neighborhood_async, _run_async, _timeline_events_from_records


class TimelineQueryService:
    """Historical provenance timeline assembly boundary."""

    def __init__(
        self,
        *,
        driver: BackendDriver | None = None,
        snapshot: object | None = None,
    ) -> None:
        self.driver = driver
        self.snapshot = snapshot

    def get_timeline(
        self,
        *,
        repo_id: str | None,
        target_id: str,
        limit: int = 50,
    ) -> TimelineResponse:
        return _run_async(
            self.get_timeline_async(
                repo_id=repo_id,
                target_id=target_id,
                limit=limit,
            )
        )

    async def get_timeline_async(
        self,
        *,
        repo_id: str | None,
        target_id: str,
        limit: int = 50,
    ) -> TimelineResponse:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if self.driver is None:
            raise RuntimeError("backend driver is required for timeline queries")
        center_record, node_records, edge_records = await _expand_backend_neighborhood_async(
            driver=self.driver,
            node_id=target_id,
            repo_id=repo_id,
            hops=2,
            edge_types=None,
        )
        events = _timeline_events_from_records(
            center_record=center_record,
            node_records=node_records,
            edge_records=edge_records,
            limit=limit,
        )
        return TimelineResponse(
            status=IMPLEMENTED_STATUS,
            message="Built a historical provenance timeline around the exact target.",
            repo_id=repo_id,
            target_id=target_id,
            events=events,
        )


def get_timeline(
    *,
    service: TimelineQueryService,
    repo_id: str | None,
    target_id: str,
    limit: int,
) -> TimelineResponse:
    return service.get_timeline(
        repo_id=repo_id,
        target_id=target_id,
        limit=limit,
    )


async def get_timeline_async(
    *,
    service: TimelineQueryService,
    repo_id: str | None,
    target_id: str,
    limit: int,
) -> TimelineResponse:
    return await service.get_timeline_async(
        repo_id=repo_id,
        target_id=target_id,
        limit=limit,
    )
