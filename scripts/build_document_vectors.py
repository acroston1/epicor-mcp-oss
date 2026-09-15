#!/usr/bin/env python3
"""OPTIONAL: embed administrator documents with a chosen sentence-transformers model.

This explicit build step may download the selected model. Query-time loads use
only the existing local/cache model. No model identifier is hardcoded.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from epicor_mcp.index.document_vectors import corpus_digest, corpus_rows


def build_document_vectors(database: Path, output: Path, model: str, *, batch_size: int = 32,
                           device: str = "cpu", local_files_only: bool = False) -> dict:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    if batch_size < 1:
        raise ValueError("batch-size must be positive")
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = corpus_rows(conn)
    finally:
        conn.close()
    if not rows:
        raise ValueError("Document database is empty; ingest documents first")
    encoder = SentenceTransformer(model, device=device, local_files_only=local_files_only)
    matrix = np.asarray(encoder.encode([row["content"] for row in rows], batch_size=batch_size,
                                      normalize_embeddings=True, show_progress_bar=True), dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(rows) or not np.isfinite(matrix).all():
        raise ValueError("Embedding model returned an invalid matrix")
    manifest = {"format": "epicor-doc-vectors-v1", "model": model,
                "dimension": matrix.shape[1], "count": len(rows),
                "normalize_embeddings": True, "encoder": "sentence-transformers.encode",
                "corpus_sha256": corpus_digest(rows),
                "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
    output.mkdir(parents=True, exist_ok=True)
    with (output / "vectors.npy.tmp").open("wb") as file:
        np.save(file, matrix, allow_pickle=False)
    (output / "vectors.npy.tmp").replace(output / "vectors.npy")
    (output / "manifest.json.tmp").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json.tmp").replace(output / "manifest.json")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/epicor_docs.db"))
    parser.add_argument("--output", type=Path, default=Path("data/document_vectors"))
    parser.add_argument("--model", required=True, help="sentence-transformers model identifier or local path")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_document_vectors(args.database, args.output, args.model,
          batch_size=args.batch_size, device=args.device, local_files_only=args.local_files_only), indent=2))


if __name__ == "__main__":
    main()
