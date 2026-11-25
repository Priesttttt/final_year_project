"""FastAPI application factory and JSON routes."""

from __future__ import annotations

import base64
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dimf.api.state import AppState, AuthenticationError, PolicyError
from dimf.common.config import Settings, load_settings
from dimf.common.models import (
    DataClass,
    FaultRequest,
    SignedSubmission,
    Submission,
    VerifyRequest,
)
from dimf.l3_verify.authlog import DuplicateRecord, RecordNotFound
from dimf.l3_verify.verifier import verify_consistency_proof, verify_record

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class DemoSubmit(BaseModel):
    agency: str = "agency-2"
    data_class: DataClass = "A"
    payload: dict[str, Any] = Field(default_factory=dict)
    with_document: bool = False


def _state(request: Request) -> AppState:
    return request.app.state.dimf  # type: ignore[attr-defined]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(settings)
        await state.start()
        app.state.dimf = state
        logger.info(
            "DIMF prototype ready: %d records, anchor batch %d, ledger %s",
            state.authlog.tree.size,
            settings.anchor_batch,
            state.ledger.backend_name,
        )
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(
        title="DIMF prototype",
        version="0.1.0",
        description=(
            "Prototype of a four-layer framework for scalable and verifiable "
            "distributed information management. Synthetic data only."
        ),
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(AuthenticationError)
    async def _auth_error(_: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc), "error": "authentication"})

    @app.exception_handler(PolicyError)
    async def _policy_error(_: Request, exc: PolicyError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc), "error": "policy"})

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {"status": "ok", "tree_size": state.authlog.tree.size}

    # -- L1/L2/L4 ingestion ----------------------------------------------
    @app.post("/records")
    async def submit_record(submission: SignedSubmission, request: Request) -> dict[str, Any]:
        state = _state(request)
        try:
            return await state.submit_signed(submission)
        except DuplicateRecord as exc:
            raise HTTPException(status_code=409, detail=f"duplicate record id: {exc}") from exc
        except (ValueError, base64.binascii.Error) as exc:
            raise HTTPException(status_code=400, detail=f"malformed submission: {exc}") from exc

    # -- L3 verifiability ------------------------------------------------
    @app.get("/log/head")
    async def log_head(request: Request) -> dict[str, Any]:
        return _state(request).authlog.head()

    @app.get("/log/stats")
    async def log_stats(request: Request) -> dict[str, Any]:
        return _state(request).authlog.stats()

    @app.get("/log/proof/{record_id}")
    async def log_proof(record_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        try:
            proof = state.authlog.proof(record_id)
        except RecordNotFound as exc:
            raise HTTPException(status_code=404, detail=f"unknown record: {record_id}") from exc
        started = time.perf_counter()
        if proof.get("record") is None:
            verification: dict[str, Any] = {"valid": False, "reason": "stored record is not valid JSON"}
        else:
            verification = verify_record(
                proof["record"], proof["index"], proof["size"], proof["proof"], proof["root"]
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        state.metrics.record_proof(len(proof["proof"]) * 32, elapsed_ms)
        proof["verification"] = verification
        proof["valid_at_generation"] = verification["valid"]
        return proof

    @app.get("/log/consistency")
    async def log_consistency(old: int, new: int, request: Request) -> dict[str, Any]:
        state = _state(request)
        try:
            result = state.authlog.consistency(old, new)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result["valid"] = verify_consistency_proof(
            old, new, result["old_root"], result["new_root"], result["proof"]
        )
        return result

    @app.post("/verify")
    async def verify(payload: VerifyRequest, request: Request) -> dict[str, Any]:
        state = _state(request)
        started = time.perf_counter()
        result = verify_record(payload.record, payload.index, payload.size, payload.proof, payload.root)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        state.metrics.record_proof(len(payload.proof) * 32, elapsed_ms)
        return {
            "valid": result["valid"],
            "reason": result["reason"],
            "leaf_hash": result["leaf_hash"],
            "computed_root": result.get("computed_root"),
        }

    @app.get("/ledger/anchors")
    async def ledger_anchors(request: Request, limit: int = 50) -> dict[str, Any]:
        state = _state(request)
        stats = state.authlog.stats()
        return {
            "stats": stats,
            "anchors": state.authlog.anchors(limit=max(1, min(limit, 500))),
            "ledger_transactions": state.ledger.transaction_count(),
            "ledger_entries": state.ledger.entries(limit=max(1, min(limit, 500))),
        }

    # -- L2 control -------------------------------------------------------
    @app.get("/cluster/status")
    async def cluster_status(request: Request) -> dict[str, Any]:
        state = _state(request)
        status = state.router.status()
        status["log"] = state.authlog.stats()
        status["ledger_transactions"] = state.ledger.transaction_count()
        return status

    @app.post("/cluster/fault")
    async def cluster_fault(fault: FaultRequest, request: Request) -> dict[str, Any]:
        state = _state(request)
        try:
            return await state.router.inject_fault(
                fault.action, fault.data_class, fault.node, fault.enabled
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- L4 evaluation plane ---------------------------------------------
    @app.get("/metrics")
    async def metrics(request: Request) -> dict[str, Any]:
        state = _state(request)
        data = state.metrics.as_dict()
        data["log"] = state.authlog.stats()
        data["policy"] = state.policy.as_dict()
        data["policy_violations"] = state.policy.violations(limit=20)
        data["synthetic"] = True
        return data

    # -- demo helpers (clearly labelled synthetic) ------------------------
    @app.get("/demo/agencies")
    async def demo_agencies(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {
            "synthetic": True,
            "members": state.identities.members(),
            "policy": state.policy.as_dict(),
            "classes": {
                "A": "monitoring/statistics (CRDT)",
                "B": "single-authority record (Raft)",
                "C": "shared fund balance (PBFT)",
            },
        }

    @app.get("/demo/records")
    async def demo_records(request: Request, limit: int = 50) -> dict[str, Any]:
        state = _state(request)
        return {
            "synthetic": True,
            "records": state.authlog.records(limit=max(1, min(limit, 200))),
        }

    @app.post("/demo/submit")
    async def demo_submit(body: DemoSubmit, request: Request) -> dict[str, Any]:
        state = _state(request)
        payload = dict(body.payload)
        payload.setdefault("beneficiary_id", f"SYNTH-BEN-{uuid.uuid4().hex[:5].upper()}")
        payload.setdefault("scheme", "SYNTH-LIVE-SCHEME")
        payload.setdefault("amount_paise", 250_000)
        payload.setdefault("note", "SYNTHETIC live demo record; not real government data")
        record_id = f"LIVE-{uuid.uuid4().hex[:12].upper()}"
        document_b64 = None
        if body.with_document:
            doc = f"SYNTHETIC live document for {record_id}\n".encode("utf-8")
            document_b64 = base64.b64encode(doc).decode("ascii")
        submission = Submission(
            record_id=record_id,
            agency=body.agency,
            data_class=body.data_class,
            timestamp=int(time.time() * 1000),
            payload=payload,
            document_b64=document_b64,
        )
        try:
            signed = state.identities.sign(submission)
            return await state.submit_signed(signed)
        except DuplicateRecord as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/demo/sign")
    async def demo_sign(submission: Submission, request: Request) -> dict[str, Any]:
        """Return a server-signed submission, so it can be POSTed to /records."""
        state = _state(request)
        try:
            signed = state.identities.sign(submission)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"signed_submission": signed.model_dump()}

    @app.post("/demo/tamper/{record_id}")
    async def demo_tamper(record_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        if not state.settings.allow_tamper_endpoint:
            raise HTTPException(status_code=403, detail="tamper endpoint disabled")
        try:
            result = state.authlog.tamper(record_id)
            proof = state.authlog.proof(record_id)
        except RecordNotFound as exc:
            raise HTTPException(status_code=404, detail=f"unknown record: {record_id}") from exc
        verification = verify_record(
            proof["record"], proof["index"], proof["size"], proof["proof"], proof["root"]
        )
        reproduced, signed = state.authlog.reproduce_root_from_stored_records()
        result.update(
            {
                "synthetic": True,
                "demo_only": True,
                "inclusion_valid_after_tamper": verification["valid"],
                "verify_reason": verification["reason"],
                "recomputed_root_from_db": reproduced,
                "signed_root": signed,
                "root_matches": reproduced == signed,
            }
        )
        return result

    return app


app = create_app()
