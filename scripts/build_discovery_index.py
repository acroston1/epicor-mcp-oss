#!/usr/bin/env python3
"""Build table/field discovery from an administrator catalogue, optionally with vectors.

Default: metadata-only. Retrieval is literal, case-insensitive substring search
in names, labels and descriptions; no model, no numpy. Pass --model to ALSO
embed every table and field description with a local sentence-transformers
model (pip install -e '.[semantic]'), or --endpoint plus --model to use an
OpenAI-compatible /v1/embeddings server you run (pip install -e '.[vectors]').
The manifest records provider, model and dimension; the server verifies them at
startup and falls back to substring search, announced, on any mismatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from epicor_mcp.discovery.store import (  # noqa: E402
    EMBED_DIM,
    FIELD_QUERY_PREFIX,
    TABLE_QUERY_PREFIX,
    compose_documents,
    remove_vectors,
    write_lexical,
    write_vectors,
)


def build_discovery_index(catalogue: dict, output: Path, embedder=None, *, batch_size: int = 32) -> dict:
    tables = catalogue.get("tables", catalogue)
    frows, fdense, flex, trows, tdense, tlex = compose_documents(tables)
    output.mkdir(parents=True, exist_ok=True)
    blocks = {row["table"]: {"offset": row["offset"], "count": row["field_count"]} for row in trows}
    (output / "fields_meta.json").write_text(json.dumps({"blocks": blocks, "rows": frows}, ensure_ascii=False), encoding="utf-8")
    (output / "tables_meta.json").write_text(json.dumps({"tables": trows}, ensure_ascii=False), encoding="utf-8")
    write_lexical(output, frows, flex, trows, tlex)
    manifest = {"format": "epicor-discovery-v1", "semantic": False,
                "source": catalogue.get("source", "administrator catalogue"),
                "table_count": len(trows), "field_count": len(frows)}
    if embedder is not None and trows:
        fmat = embedder.encode(fdense, batch_size=batch_size)
        tmat = embedder.encode(tdense, batch_size=batch_size)
        if fmat.shape[1] != tmat.shape[1]:
            raise ValueError("field and table embeddings disagree on dimension")
        write_vectors(output, fmat, tmat)
        manifest.update({"semantic": True, "provider": embedder.provider, "model": embedder.model,
                         "dim": int(tmat.shape[1]), "field_prefix": FIELD_QUERY_PREFIX,
                         "table_prefix": TABLE_QUERY_PREFIX})
    else:
        # A metadata-only rebuild must not leave an older build's arrays behind.
        remove_vectors(output)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def make_embedder(args: argparse.Namespace):
    """The provider the flags select, or ``None`` for a metadata-only build."""
    if not args.model:
        return None
    if args.endpoint:
        from epicor_mcp.discovery.embeddings import EndpointEmbedder
        return EndpointEmbedder(args.endpoint, args.model, dim=args.dim, timeout=600.0)
    from epicor_mcp.discovery.embeddings import LocalEmbedder
    return LocalEmbedder(args.model, device=args.device, local_files_only=args.local_files_only, progress=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=Path("data/schema_catalogue.json"))
    parser.add_argument("--output", type=Path, default=Path("data/discovery_index"))
    parser.add_argument("--model", default="",
                        help="OPTIONAL: sentence-transformers model id or local path; with --endpoint, the model name to request")
    parser.add_argument("--endpoint", default="",
                        help="OPTIONAL: OpenAI-compatible /v1/embeddings URL you operate; requires --model")
    parser.add_argument("--dim", type=int, default=EMBED_DIM,
                        help=f"truncate ENDPOINT vectors to this many dimensions (default {EMBED_DIM})")
    parser.add_argument("--device", default="cpu", help="device for a local model")
    parser.add_argument("--local-files-only", action="store_true", help="never download a local model")
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    if args.endpoint and not args.model:
        parser.error("--endpoint requires --model")
    if args.batch_size < 1 or args.dim < 1:
        parser.error("--batch-size and --dim must be positive")
    embedder = make_embedder(args)
    try:
        print(json.dumps(build_discovery_index(json.loads(args.input.read_text(encoding="utf-8")), args.output,
                                               embedder, batch_size=args.batch_size), indent=2))
    finally:
        if embedder is not None:
            embedder.close()


if __name__ == "__main__":
    main()
