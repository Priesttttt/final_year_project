"""Pydantic data models shared by the API, L1, L2 and L3.

Record layout entering the authenticated log (single source of truth):

    {
      "record_id": str,
      "agency": str,
      "data_class": "A" | "B" | "C",
      "timestamp": int,               # Unix epoch milliseconds
      "payload": {...},               # arbitrary small JSON object
      "document": {"sha256": str, "size": int} | None
    }

The raw document bytes never enter the log; only the content hash and size.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DataClass = Literal["A", "B", "C"]
ALL_DATA_CLASSES: tuple[DataClass, ...] = ("A", "B", "C")


class DocumentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class Record(BaseModel):
    """The canonical record that is hashed into the authenticated log."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=128)
    agency: str = Field(min_length=1, max_length=64)
    data_class: DataClass
    timestamp: int = Field(ge=0)
    payload: dict[str, Any]
    document: DocumentRef | None = None

    def canonical_dict(self) -> dict[str, Any]:
        """Plain dict with stable shape (documents omitted when absent)."""
        data: dict[str, Any] = {
            "record_id": self.record_id,
            "agency": self.agency,
            "data_class": self.data_class,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        if self.document is not None:
            data["document"] = {"sha256": self.document.sha256, "size": self.document.size}
        return data


class Submission(BaseModel):
    """An agency-authored unsigned submission."""

    record_id: str = Field(min_length=1, max_length=128)
    agency: str = Field(min_length=1, max_length=64)
    data_class: DataClass
    timestamp: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    document_b64: str | None = None
    signature: str | None = None

    def signed_payload(self) -> dict[str, Any]:
        """Payload covered by the signature (everything except signature)."""
        data = self.model_dump(exclude={"signature"}, mode="json")
        return data


class SignedSubmission(Submission):
    """A submission that must carry a signature for POST /records."""

    signature: str = Field(min_length=1)


class VerifyRequest(BaseModel):
    """Input for the standalone public verifier."""

    record: dict[str, Any]
    index: int = Field(ge=0)
    size: int = Field(gt=0)
    proof: list[str]
    root: str = Field(min_length=64, max_length=64)


class VerifyResponse(BaseModel):
    valid: bool
    reason: str
    leaf_hash: str
    computed_root: str | None = None


class SubmitResponse(BaseModel):
    record_id: str
    status: str
    data_class: DataClass
    log_index: int | None
    tree_size: int | None
    latency_ms: float
    anchored: bool


class FaultRequest(BaseModel):
    action: Literal["crash", "recover", "partition", "heal", "byzantine"]
    data_class: DataClass | None = None
    node: str | None = None
    enabled: bool = True
