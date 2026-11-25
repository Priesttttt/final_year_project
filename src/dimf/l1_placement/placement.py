"""L1 data placement: off-chain documents, small records, class routing."""

from __future__ import annotations

from typing import Any, Protocol

from dimf.common.models import Record, Submission
from dimf.l1_placement.blobstore import BlobStore


class SyncRouterProtocol(Protocol):
    async def route(self, record: Record) -> Any:  # pragma: no cover - protocol
        ...


class PlacementService:
    """Turns a submission into a log-ready record and routes it to L2."""

    def __init__(self, blob_store: BlobStore, router: SyncRouterProtocol) -> None:
        self.blob_store = blob_store
        self.router = router

    def prepare(self, submission: Submission, document: bytes | None = None) -> Record:
        doc_ref = self.blob_store.put(document) if document is not None else None
        return Record(
            record_id=submission.record_id,
            agency=submission.agency,
            data_class=submission.data_class,
            timestamp=submission.timestamp,
            payload=submission.payload,
            document=doc_ref,
        )

    async def place_and_route(self, submission: Submission, document: bytes | None = None) -> Any:
        record = self.prepare(submission, document)
        return await self.router.route(record)
