"""In-memory reader for the nightly-built ``menu_security.db``.

The store is the *structural* half of the two-tier design: it answers "which
services do these menus touch?" and "what is the justified baseline?".  It loads
the whole (small) SQLite map into memory on construction and, on every tenant
refresh, ``maybe_reload()`` picks up a nightly rebuild by comparing the file
mtime — no server restart needed.

Fail-closed is the contract: a missing / unreadable db reports
``is_loaded() == False`` and every lookup returns an empty set rather than
raising, so the authorizer denies rather than crashes.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


class MenuMapStore:
    """Loads and serves the menu->service structural map from ``menu_security.db``."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._mtime: float | None = None
        self._menu_services: dict[str, set[str]] = {}
        self._baseline: dict[str, str] = {}
        self._loaded = False
        self._load()

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    def _load(self) -> bool:
        """(Re)load the whole map into memory.  Returns True on a successful load."""
        if not self._path.exists():
            self._menu_services = {}
            self._baseline = {}
            self._loaded = False
            self._mtime = None
            return False

        menu_services: dict[str, set[str]] = {}
        baseline: dict[str, str] = {}
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            try:
                for menu_id, service_id in conn.execute(
                    "SELECT menu_id, service_id FROM menu_services"
                ):
                    menu_services.setdefault(menu_id, set()).add(service_id)
                for service_id, justification in conn.execute(
                    "SELECT service_id, justification FROM baseline_services"
                ):
                    baseline[service_id] = justification or ""
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("menu_security.db unreadable (%s) — failing closed", exc)
            self._menu_services = {}
            self._baseline = {}
            self._loaded = False
            return False

        self._menu_services = menu_services
        self._baseline = baseline
        self._loaded = True
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None
        return True

    def maybe_reload(self) -> bool:
        """Reload if the db file's mtime advanced since the last load.

        Returns ``True`` when a reload actually happened.  Also loads for the
        first time if the db has appeared since construction.
        """
        if not self._path.exists():
            if self._loaded:
                # db disappeared — fail closed.
                self._load()
            return False
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return False
        if self._mtime is None or mtime > self._mtime:
            return self._load()
        return False

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    def is_loaded(self) -> bool:
        """``False`` => the map is missing/unreadable => callers must fail closed."""
        return self._loaded

    def services_for_menus(self, menu_ids: Iterable[str]) -> set[str]:
        """Union of the services mapped to any of ``menu_ids``."""
        out: set[str] = set()
        for menu_id in menu_ids:
            out |= self._menu_services.get(menu_id, set())
        return out

    def baseline(self) -> set[str]:
        """The justified baseline services granted to every (non-error) user."""
        return set(self._baseline)
