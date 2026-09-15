"""Optional, model-bound document vectors built from administrator documents.

Only this opt-in path imports numpy/sentence-transformers. Runtime model loads
are cache/local-only; downloads happen solely when the build script is invoked.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


def corpus_digest(rows: list[dict]) -> str:
    content = [[row["id"], row["title"], row["content"]] for row in rows]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def corpus_rows(conn: sqlite3.Connection) -> list[dict]:
    return [{"id": row[0], "title": row[1], "content": row[2]} for row in conn.execute(
        "SELECT id, title, content FROM doc_chunks ORDER BY id")]


class DocumentVectors:
    def __init__(self, root: Path, conn: sqlite3.Connection, *, model: str | None = None) -> None:
        import numpy as np
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("format") != "epicor-doc-vectors-v1":
            raise ValueError("Unsupported document vector manifest")
        recorded_model = self.manifest.get("model")
        if not isinstance(recorded_model, str) or not recorded_model:
            raise ValueError("Vector manifest has no embedding model")
        if model and model != recorded_model:
            raise ValueError("Configured embedding model differs from the built index; rebuild with that model")
        self._model_name = recorded_model
        rows = corpus_rows(conn)
        if corpus_digest(rows) != self.manifest.get("corpus_sha256"):
            raise ValueError("Documents changed since vector build; rebuild the document vectors")
        self._matrix = np.load(root / "vectors.npy", allow_pickle=False, mmap_mode="r")
        expected = (len(rows), self.manifest.get("dimension"))
        if self._matrix.ndim != 2 or self._matrix.shape != expected or not np.isfinite(self._matrix).all():
            raise ValueError("Document vector dimensions/count do not match the manifest")
        if not isinstance(expected[1], int) or expected[1] <= 0:
            raise ValueError("Invalid vector dimension")
        self._positions = {row["id"]: index for index, row in enumerate(rows)}
        self._model: Any = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._model = None
            mapping = getattr(self._matrix, "_mmap", None)
            if mapping is not None:
                mapping.close()

    def search(self, query: str, rows: list[dict], limit: int) -> list[dict]:
        import numpy as np
        if not rows:
            return []
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name, device="cpu", local_files_only=True)
            # Use the SAME encode call and normalization as the build script.
            vec = np.asarray(self._model.encode([query], normalize_embeddings=True,
                                               show_progress_bar=False), dtype=np.float32)
        if vec.shape != (1, self._matrix.shape[1]) or not np.isfinite(vec).all():
            raise ValueError("Query embedding dimension does not match the built document index")
        scores = self._matrix @ vec[0]
        ranked = sorted(rows, key=lambda row: (-float(scores[self._positions[row["id"]]]), row["id"]))
        return [dict(row, score=round(float(scores[self._positions[row["id"]]]), 6)) for row in ranked[:limit]]
