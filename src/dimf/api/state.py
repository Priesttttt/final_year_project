"""Application composition root: wires L1-L4 together."""

from __future__ import annotations

import base64
import random

from dimf.common.config import Settings
from dimf.common.metrics import MetricsCollector
from dimf.common.models import Submission
from dimf.l1_placement.blobstore import BlobStore
from dimf.l1_placement.placement import PlacementService
from dimf.l2_sync.crdt import CRDTCluster
from dimf.l2_sync.network import SimulatedNetwork
from dimf.l2_sync.pbft import PBFTCluster
from dimf.l2_sync.raft import RaftCluster
from dimf.l2_sync.router import SyncRouter
from dimf.l3_verify.anchor import make_ledger
from dimf.l3_verify.authlog import AuthLog
from dimf.l4_identity.identity import IdentityRegistry
from dimf.l4_identity.policy import PolicyEngine


class AuthenticationError(Exception):
    """Unsigned, wrongly signed or non-member submission."""


class PolicyError(Exception):
    """Authenticated agency is not allowed to write this data class."""


class AppState:
    """All long-lived objects for one process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        assert settings.db_path is not None and settings.blob_dir is not None
        self.metrics = MetricsCollector()
        self.network = SimulatedNetwork(
            latency_ms=settings.network_latency_ms,
            jitter_ms=settings.network_jitter_ms,
            drop_rate=settings.network_drop_rate,
            seed=settings.seed,
        )
        self.identities = IdentityRegistry(settings.db_path, settings.identity_master_seed)
        self.policy = PolicyEngine(settings.db_path, settings.policy_spec)
        self.blobs = BlobStore(settings.blob_dir)
        self.ledger = make_ledger(settings)
        self.authlog = AuthLog(
            settings.db_path,
            self.ledger,
            anchor_batch=settings.anchor_batch,
            key_seed=settings.log_key_seed,
            metrics=self.metrics,
        )
        self.crdt = CRDTCluster(
            self.network, n=settings.crdt_nodes, gossip_interval_s=settings.crdt_gossip_s
        )
        self.raft = RaftCluster(
            self.network,
            n=settings.raft_nodes,
            heartbeat_s=settings.raft_heartbeat_s,
            election_min_s=settings.raft_election_min_s,
            election_max_s=settings.raft_election_max_s,
            seed=settings.seed,
        )
        self.pbft = PBFTCluster(self.network, n=settings.pbft_nodes)
        self.router = SyncRouter(
            self.network, self.crdt, self.raft, self.pbft, self.authlog, self.metrics
        )
        self.placement = PlacementService(self.blobs, self.router)
        self.metrics.set_ledger(
            backend=self.ledger.backend_name,
            transaction_count=self.ledger.transaction_count(),
            anchor_batch=settings.anchor_batch,
            log_size=self.authlog.tree.size,
        )

    async def start(self) -> None:
        self.router.start()
        if self.settings.seed_on_start and self.authlog.tree.size == 0:
            await self.seed_demo(self.settings.demo_seed_records)

    async def stop(self) -> None:
        await self.router.stop()
        await self.network.close()
        self.authlog.close()

    async def submit_signed(self, submission: Submission) -> dict[str, object]:
        """Shared path for POST /records and the dashboard demo helper."""
        if not submission.signature:
            raise AuthenticationError("missing signature")
        from dimf.common.models import SignedSubmission

        signed = SignedSubmission(**submission.model_dump())
        ok, reason = self.identities.verify_submission(signed)
        if not ok:
            self.metrics.incr("signature_rejections")
            raise AuthenticationError(reason)
        allowed, reason = self.policy.check(submission.agency, submission.data_class, submission.record_id)
        if not allowed:
            self.metrics.incr("policy_rejections")
            raise PolicyError(reason)
        document = base64.b64decode(submission.document_b64) if submission.document_b64 else None
        result = await self.placement.place_and_route(submission, document)
        return {
            "record_id": result.record_id,
            "status": "committed",
            "data_class": result.data_class,
            "log_index": result.index,
            "tree_size": result.tree_size,
            "latency_ms": round(result.latency_ms, 3),
            "anchored": result.anchored,
            "protocol": result.protocol,
            "messages_for_entry": result.messages_for_entry,
        }

    async def seed_demo(self, count: int = 24) -> int:
        """Create deterministic synthetic records (explicitly labelled)."""
        rng = random.Random(self.settings.seed)
        schemes = ["SYNTH-SCHOLARSHIP", "SYNTH-HOUSING", "SYNTH-HEALTH", "SYNTH-FOOD"]
        districts = ["SYNTH-DISTRICT-A", "SYNTH-DISTRICT-B", "SYNTH-DISTRICT-C"]
        agencies = ["agency-1", "agency-2", "agency-3"]
        made = 0
        for i in range(count):
            cls = ["A", "B", "C"][i % 3]
            # Respect the demo policy: only agency-1 may write class B.
            agency = "agency-1" if cls == "B" else agencies[i % len(agencies)]
            record_id = f"SYN-{i:05d}"
            payload = {
                "beneficiary_id": f"SYNTH-BEN-{rng.randint(10000, 99999)}",
                "scheme": rng.choice(schemes),
                "amount_paise": rng.randint(10_000, 900_000),
                "district": rng.choice(districts),
                "note": "SYNTHETIC demo record; not real government data",
            }
            document = None
            document_b64 = None
            if i % 4 == 0:
                document = f"SYNTHETIC DOCUMENT {i} for {record_id}\n".encode("utf-8")
                document_b64 = base64.b64encode(document).decode("ascii")
            submission = Submission(
                record_id=record_id,
                agency=agency,
                data_class=cls,  # type: ignore[arg-type]
                timestamp=1_700_000_000_000 + i * 60_000,
                payload=payload,
                document_b64=document_b64,
            )
            try:
                signed = self.identities.sign(submission)
                await self.submit_signed(signed)
                made += 1
            except Exception:
                # A seeded record must never take the app down; log and skip.
                import logging

                logging.getLogger(__name__).exception("failed to seed %s", record_id)
        return made
