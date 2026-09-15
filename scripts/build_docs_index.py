#!/usr/bin/env python3
"""Import your own documents. Embeddings are optional and built separately.

Supported sources: UTF-8 .txt/.md/.rst; JSON objects/lists with title, content,
optional source_type/url; .pdf with the optional `documents` dependency extra.
No web crawling, tenant credentials, or vendor content is bundled or fetched.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE doc_chunks (
 id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, source_file TEXT NOT NULL,
 title TEXT NOT NULL, section TEXT NOT NULL, content TEXT NOT NULL,
 page_start INTEGER, url TEXT NOT NULL
);
"""


def chunk_text(text: str, chunk_size: int = 1800, overlap: int = 200) -> list[str]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Require chunk_size > overlap >= 0")
    text = text.strip()
    chunks = []
    for start in range(0, len(text), chunk_size - overlap):
        chunk = text[start:start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(text):
            break
    return chunks


def read_documents(path: Path, root: Path) -> Iterator[dict]:
    common = {"source_file": path.relative_to(root).as_posix(), "title": path.stem,
              "source_type": "document", "section": "", "page_start": None, "url": ""}
    if path.suffix.lower() in {".txt", ".md", ".rst"}:
        yield dict(common, content=path.read_text(encoding="utf-8"))
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data if isinstance(data, list) else [data]
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("content"), str):
                raise ValueError(f"{path}: each document must contain a string content field")
            row = dict(common)
            for key in ("title", "content", "source_type", "section", "url"):
                if key in record:
                    if not isinstance(record[key], str):
                        raise ValueError(f"{path}: {key} must be a string")
                    row[key] = record[key]
            yield row
    elif path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError('PDF input requires: pip install -e ".[documents]"') from exc
        reader = PdfReader(path)
        for number, page in enumerate(reader.pages, 1):
            yield dict(common, source_type="pdf", content=page.extract_text() or "", page_start=number)


def build_docs_index(input_dir: str | Path, output: str | Path, *, chunk_size: int = 1800,
                     overlap: int = 200) -> dict[str, int]:
    root, target = Path(input_dir), Path(output)
    if not root.is_dir():
        raise ValueError(f"Document input directory does not exist: {root}")
    chunk_text("", chunk_size, overlap)  # validate even for an empty corpus
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
                   and path.suffix.lower() in {".txt", ".md", ".rst", ".json", ".pdf"})
    if not files:
        raise ValueError("No supported input documents found")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(fd)
    conn = sqlite3.connect(name)
    count = 0
    try:
        conn.executescript(SCHEMA)
        for path in files:
            for document in read_documents(path, root):
                for chunk in chunk_text(document["content"], chunk_size, overlap):
                    row = dict(document, content=chunk)
                    conn.execute("INSERT INTO doc_chunks(source_type,source_file,title,section,content,page_start,url) "
                                 "VALUES(:source_type,:source_file,:title,:section,:content,:page_start,:url)", row)
                    count += 1
        if not count:
            raise ValueError("No extractable text found in input documents")
        conn.commit()
        conn.close()
        os.replace(name, target)
    except BaseException:
        conn.close()
        Path(name).unlink(missing_ok=True)
        raise
    return {"files": len(files), "chunks": count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", default=Path("data/epicor_docs.db"), type=Path)
    parser.add_argument("--chunk-size", type=int, default=1800)
    parser.add_argument("--overlap", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(build_docs_index(args.input_dir, args.output, chunk_size=args.chunk_size,
                                     overlap=args.overlap)))


if __name__ == "__main__":
    main()
