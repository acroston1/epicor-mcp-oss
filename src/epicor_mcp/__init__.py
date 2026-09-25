"""Epicor Kinetic MCP Server with department-level RBAC."""

from __future__ import annotations


def _read_version() -> str:
    """The one version string. ``pyproject.toml`` is the source of truth.

    A source checkout (including an editable install, whose metadata goes
    stale until it is reinstalled) reads the file; a wheel install has no
    ``pyproject.toml`` beside the package and reads its metadata instead.
    Nothing else in the tree may hard-code the version.
    """
    import re
    from pathlib import Path

    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("epicor-mcp-oss")
    except (ImportError, PackageNotFoundError):
        return "0+unknown"


__version__ = _read_version()
