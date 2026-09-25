"""The server's reported version is read from pyproject.toml, never hard-coded."""

from __future__ import annotations

import re
from pathlib import Path

import epicor_mcp

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text()
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert epicor_mcp.__version__ == declared


def test_server_does_not_hard_code_a_version():
    src = (ROOT / "src" / "epicor_mcp" / "server.py").read_text()
    assert not re.search(r'version["\']?\s*[:=]\s*["\']\d+\.\d+\.\d+', src)
