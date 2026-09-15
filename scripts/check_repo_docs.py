#!/usr/bin/env python3
"""Check public documentation and agent routing without network or dependencies.

Validates paired guides, local Markdown links (including heading fragments),
quoted source-file references, and retired private-reference conventions.
Generated operator data is excluded.
Run from any directory; --root also supports checking an extracted source archive.
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import tokenize
from pathlib import Path
from urllib.parse import unquote, urlsplit


MAIN_DIRS = ("", "src", "src/epicor_mcp", "scripts", "tests", "docs", "deploy", "data", "bridge")
SOURCE_DIRS = ("src", "scripts", "tests", "docs", "deploy", "bridge")
EXCLUDED_DIRS = {"build", "dist", "__pycache__", "venv", "node_modules"}
LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^\s)]+)(?:\s+[\"'][^\n]*?[\"'])?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)")
SOURCE_REFERENCE = re.compile(
    r"(?P<ticks>`{1,2})(?P<path>(?:src|tests|scripts|docs|bridge|deploy)/"
    r"[\w./-]+\.(?:py|md|json|ya?ml|toml|txt|sh|bat|ps1))"
    r"(?:::[\w.]+)?(?P=ticks)"
)
RETIRED = re.compile(
    r"\bdesign\s*§|\bprobe\s+\d+|\bv[123]_design\b|"
    r"~/(?:Documents|\.claude|\.codex)/|tests/bench/|docs/probes/|"
    r"tests/harness/(?:analyse|capture)\.py", re.IGNORECASE,
)


def _public_files(root: Path) -> list[Path]:
    paths = set(root.glob("*.md"))
    paths.update(path for name in ("README.md", "AGENTS.md", "CLAUDE.md")
                 if (path := root / "data" / name).is_file())
    for directory in SOURCE_DIRS:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part in EXCLUDED_DIRS for part in relative.parts):
                continue
            if path.suffix in {".md", ".py"} and path.is_file():
                paths.add(path)
    return sorted(paths)


def _prose(text: str):
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if not fence:
            yield number, line


def _anchors(text: str) -> set[str]:
    anchors = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', text))
    counts: dict[str, int] = {}
    for _, line in _prose(text):
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading[1])
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def _python_prose(text: str):
    """Yield actual comments and docstrings, never illustrative string values."""
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string
        tree = ast.parse(text)
    except (SyntaxError, tokenize.TokenError):
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is not None:
            start = node.body[0].lineno
            for offset, line in enumerate(doc.splitlines()):
                yield start + offset, line


def check_repo_docs(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    guide_dirs = {root / directory for directory in MAIN_DIRS}
    guide_dirs.update(path.parent for path in (root / "src/epicor_mcp").rglob("__init__.py"))
    files = _public_files(root)
    guide_dirs.update(path.parent for path in files if path.name in {"AGENTS.md", "CLAUDE.md"})
    for directory in sorted(guide_dirs):
        guides = [directory / name for name in ("AGENTS.md", "CLAUDE.md")]
        for guide in guides:
            label = guide.relative_to(root)
            if not guide.is_file() or guide.is_symlink():
                errors.append(f"{label}: missing regular guide file")
            elif len(guide.read_text(encoding="utf-8").splitlines()) > 60:
                errors.append(f"{label}: exceeds 60 lines")
        if all(guide.is_file() for guide in guides) and guides[0].read_bytes() != guides[1].read_bytes():
            errors.append(f"{directory.relative_to(root)}: guide pair differs")

    anchor_cache: dict[Path, set[str]] = {}
    for path in files:
        label = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        if label.as_posix() != "scripts/check_repo_docs.py":
            for number, line in enumerate(text.splitlines(), 1):
                if RETIRED.search(line):
                    errors.append(f"{label}:{number}: retired private-reference convention")
        prose = _python_prose(text) if path.suffix == ".py" else _prose(text)
        for number, line in prose:
            for reference in SOURCE_REFERENCE.finditer(line):
                target = reference["path"]
                if any(part in EXCLUDED_DIRS for part in Path(target).parts):
                    continue  # Build outputs are created by the documented commands.
                destination = (root / target).resolve()
                if not destination.is_relative_to(root):
                    errors.append(f"{label}:{number}: local reference escapes repository: {target}")
                elif not destination.is_file():
                    errors.append(f"{label}:{number}: missing local reference: {target}")
        if path.suffix != ".md":
            continue
        for number, line in _prose(text):
            targets = [match[1] for match in LINK.finditer(line)]
            reference = REFERENCE.match(line)
            if reference:
                targets.append(reference[1])
            for target in targets:
                parsed = urlsplit(target.strip("<>"))
                if parsed.scheme or parsed.netloc:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                if not destination.is_relative_to(root):
                    errors.append(f"{label}:{number}: local link escapes repository: {target}")
                elif not destination.exists():
                    errors.append(f"{label}:{number}: missing local link: {target}")
                elif parsed.fragment and destination.suffix == ".md":
                    if destination not in anchor_cache:
                        anchor_cache[destination] = _anchors(destination.read_text(encoding="utf-8"))
                    if unquote(parsed.fragment) not in anchor_cache[destination]:
                        errors.append(f"{label}:{number}: missing heading: {target}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check_repo_docs(args.root)
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("Documentation checks passed: paired guides, local links, and public references.")


if __name__ == "__main__":
    main()
