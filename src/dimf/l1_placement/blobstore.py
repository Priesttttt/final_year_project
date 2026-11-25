"""Content-addressed off-chain blob store (L1).

Documents are stored by the SHA-256 of their bytes under a two-level
directory.  The authenticated log only receives the hash and size.  Writes
are atomic (temporary file + ``os.replace``), which works on Linux, macOS and
Windows.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from dimf.common.models import DocumentRef


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid SHA-256 hex digest")
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> DocumentRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".blob-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        return DocumentRef(sha256=digest, size=len(data))

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def get(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("blob content does not match its address (corruption)")
        return data

    def path_for(self, digest: str) -> Path:
        return self._path(digest)
