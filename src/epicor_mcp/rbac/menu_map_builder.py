"""Offline builder for ``menu_security.db``.

Two layers:

* :func:`write_menu_map_db` — the **atomic writer** used by both the nightly
  build and the test fixtures.  It lays down exactly the menu-map database
  schema and swaps the file in via ``os.replace`` so readers never see a
  half-written db.
* :class:`MenuMapBuilder` — orchestrates the per-launchable-menu **strategy
  chain** (override -> dashboard -> metafx ExportApp -> UIProc/App heuristic ->
  unmapped), normalizes service ids, drops ones absent from the service index,
  and enforces the coverage safety valve before a replace.

The live MetaFX/Menu fetching is done by :class:`EpicorMetaClient` (sync httpx,
LIVE, read-only, ExportApp results cached per app id).  The extraction /
normalization helpers are pure so they can be reasoned about without a network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from epicor_mcp.rbac.epicor_authz import _more_pages

logger = logging.getLogger(__name__)

# A concrete Epicor service reference: Erp.BO.XxxSvc / Ice.BO.XxxSvc.
_SERVICE_RE = re.compile(r"\b((?:Erp|Ice)\.BO\.[A-Za-z0-9_]+?Svc)\b")
# An explicit "svc": "Erp.BO.XxxSvc" reference inside an exported app definition.
_SVC_FIELD_RE = re.compile(r'"svc"\s*:\s*"([^"]+)"')
# Line + block comment strippers for JSONC (the ExportApp Files are JSON-with-comments).
_LINE_COMMENT_RE = re.compile(r"//[^\n\r]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE menus (
    menu_id        TEXT PRIMARY KEY,
    parent_menu_id TEXT,
    menu_desc      TEXT,
    sec_code       TEXT,
    program        TEXT,
    app_id         TEXT,
    enabled        INTEGER,
    hidden         INTEGER,
    map_strategy   TEXT,
    map_confidence REAL
);
CREATE TABLE menu_services (
    menu_id    TEXT,
    service_id TEXT,
    source     TEXT,
    PRIMARY KEY (menu_id, service_id)
);
CREATE INDEX idx_menu_services_service ON menu_services (service_id);
CREATE TABLE baseline_services (
    service_id    TEXT PRIMARY KEY,
    justification TEXT
);
CREATE TABLE unmapped_programs (
    program         TEXT PRIMARY KEY,
    menu_count      INTEGER,
    sample_menu_ids TEXT
);
"""


# --------------------------------------------------------------------------- #
# Atomic writer (menu-map database schema)
# --------------------------------------------------------------------------- #

def write_menu_map_db(
    path: str | Path,
    *,
    menus: Iterable[dict],
    menu_services: Iterable[tuple[str, str, str]],
    baseline_services: Iterable[tuple[str, str]] = (),
    unmapped_programs: Iterable[tuple[str, int, str]] = (),
    meta: dict | None = None,
) -> Path:
    """Write a fresh ``menu_security.db`` at ``path`` atomically.

    Parameters mirror the five tables.  ``menus`` rows are dicts carrying the
    :func:`menu record <MenuMapBuilder._menu_record>` keys; the list params are
    plain tuples.  The db is built in a sibling ``*.tmp`` file and swapped in with
    ``os.replace`` so a concurrent reader sees either the old or the new db, never
    a partial one.
    """
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    if tmp.exists():
        tmp.unlink()

    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(_DDL)
        conn.executemany(
            "INSERT OR REPLACE INTO menus (menu_id, parent_menu_id, menu_desc, "
            "sec_code, program, app_id, enabled, hidden, map_strategy, map_confidence) "
            "VALUES (:menu_id, :parent_menu_id, :menu_desc, :sec_code, :program, "
            ":app_id, :enabled, :hidden, :map_strategy, :map_confidence)",
            [_menu_row_params(m) for m in menus],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO menu_services (menu_id, service_id, source) "
            "VALUES (?, ?, ?)",
            list(menu_services),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO baseline_services (service_id, justification) "
            "VALUES (?, ?)",
            list(baseline_services),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO unmapped_programs (program, menu_count, "
            "sample_menu_ids) VALUES (?, ?, ?)",
            list(unmapped_programs),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in (meta or {}).items()],
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp, path)
    return path


def _menu_row_params(m: dict) -> dict:
    """Coerce a menu record dict into the exact columns/typing ``menus`` expects."""
    return {
        "menu_id": m["menu_id"],
        "parent_menu_id": m.get("parent_menu_id", "") or "",
        "menu_desc": m.get("menu_desc", "") or "",
        "sec_code": m.get("sec_code", "") or "",
        "program": m.get("program", "") or "",
        "app_id": m.get("app_id", "") or "",
        "enabled": 1 if m.get("enabled", True) else 0,
        "hidden": 1 if m.get("hidden", False) else 0,
        "map_strategy": m.get("map_strategy", "unmapped"),
        "map_confidence": float(m.get("map_confidence", 0.0)),
    }


# --------------------------------------------------------------------------- #
# Pure extraction / normalization helpers
# --------------------------------------------------------------------------- #

def strip_jsonc_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments so JSONC parses as JSON."""
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def normalize_service(name: str) -> str:
    """Canonicalize a service id: trim, collapse a case-insensitive ``svc``
    suffix (including doubled ``svcSvc``) to a single ``Svc``.

    Prefix/suffix casing is fixed here; the *middle* casing (e.g. ``Partsvc`` vs
    ``PartSvc``) can only be recovered against the service index — see
    :func:`_canonical_key` / :meth:`MenuMapBuilder._resolve_service`.
    """
    name = (name or "").strip()
    if not name:
        return ""
    low = name.lower()
    if low.startswith(("erp.bo.", "ice.bo.")):
        core = name
        while core.lower().endswith("svc"):
            core = core[:-3]
        return core + "Svc"
    return name


def _canonical_key(name: str) -> str | None:
    """A case/suffix-insensitive lookup key for a BO service ref, or ``None`` if
    the string is not a ``Erp.BO.``/``Ice.BO.`` reference at all.

    ``ERp.BO.Partsvc`` and ``Erp.BO.PartsvcSvc`` both key to ``erp.bo.partsvc``,
    matching the index's ``Erp.BO.PartSvc``.  Non-BO garbage like ``%svc%`` or
    ``Erp.BO.SNTRanSearch`` (no Svc suffix) keys out / does not resolve.
    """
    low = (name or "").strip().lower()
    if not (low.startswith("erp.bo.") or low.startswith("ice.bo.")):
        return None
    while low.endswith("svc"):
        low = low[:-3]
    return low + "svc"


def extract_services_from_export(files: dict[str, str]) -> set[str]:
    """Pull every service reference out of an ExportApp ``Files`` payload.

    Prefers explicit ``"svc": "Erp.BO.XxxSvc"`` fields, then falls back to a
    regex over the raw (comment-stripped) text to catch service ids referenced
    elsewhere in the app definition.
    """
    services: set[str] = set()
    for content in (files or {}).values():
        if not content:
            continue
        text = strip_jsonc_comments(content)
        for m in _SVC_FIELD_RE.findall(text):
            services.add(normalize_service(m))
        for m in _SERVICE_RE.findall(text):
            services.add(normalize_service(m))
    return {s for s in services if s}


def program_stem(program: str) -> str:
    """The bare app stem of a menu Program / dll name.

    ``Erp.UI.APInvoiceEntry`` -> ``APInvoiceEntry``;
    ``Erp.UIProc.PartWhereUsed`` -> ``PartWhereUsed``;
    ``Erp.Proc.PartWhereUsed.dll`` -> ``PartWhereUsed``.
    """
    stem = (program or "").strip()
    if not stem:
        return ""
    if stem.lower().endswith(".dll"):
        stem = stem[:-4]
    return stem.rsplit(".", 1)[-1]


def is_dashboard_program(program: str) -> bool:
    """A dashboard-launcher program (assigns Ice.BO.DashBoardSvc)."""
    p = (program or "").lower()
    return "dashboard" in p or p.startswith("bpm.")


# --------------------------------------------------------------------------- #
# Live MetaFX / Menu client (sync, LIVE, read-only)
# --------------------------------------------------------------------------- #

class EpicorMetaClient:
    'Sync client for the offline build: menus + MetaFX app registry/export.'

    _APP_VIEW_REQUEST = {
        "request": {
            "Type": "view",
            "SubType": "",
            "SearchText": "",
            "IncludeAllLayers": True,
            "IncludePersLayers": False,
        }
    }

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        api_key: str,
        *,
        cache_dir: str | Path | None = None,
        throttle_seconds: float = 0.2,
        timeout: float = 60.0,
    ) -> None:
        import httpx  # local import: only the offline build needs it sync

        if not base_url.endswith("/"):
            base_url += "/"
        self._base_url = base_url
        self._username = username
        self._password = password
        self._api_key = api_key
        self._throttle = throttle_seconds
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._token: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EpicorMetaClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- auth --------------------------------------------------------------- #

    def _token_url(self) -> str:
        return self._base_url.split("api/v2/odata/")[0] + "TokenResource.svc/"

    def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        resp = self._client.post(
            self._token_url(),
            data={
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            },
        )
        resp.raise_for_status()
        self._token = resp.json().get("AccessToken")
        return self._token or ""

    def _headers(self, force_token: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token(force=force_token)}",
            "X-API-Key": self._api_key,
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        for attempt in range(2):
            resp = self._client.post(
                path, json=body, headers=self._headers(force_token=(attempt == 1))
            )
            if resp.status_code in (401, 403) and attempt == 0:
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"POST {path} failed after retry")

    # -- menus -------------------------------------------------------------- #

    def fetch_menus(self, page_size: int = 1000) -> list[dict]:
        """All Ice.BO.Menu rows (raw dicts), paged.

        pageSize=0 400s in the original deployment, so the page size is always positive; the
        loop drives on ``parameters.morePages`` (MenuSvc returns variable-length
        pages, so short-page detection would truncate the sweep).
        """
        out: list[dict] = []
        page = 1
        while True:
            js = self._post(
                "Ice.BO.MenuSvc/GetRows",
                {"whereClauseMenu": "", "pageSize": page_size, "absolutePage": page},
            )
            out.extend(js.get("returnObj", {}).get("Menu", []) or [])
            if not _more_pages(js):
                break
            page += 1
        return out

    # -- metafx ------------------------------------------------------------- #

    def get_applications(self) -> list[dict]:
        """The Kinetic app registry (Ice.LIB.MetaFXSvc/GetApplications)."""
        js = self._post("Ice.LIB.MetaFXSvc/GetApplications", dict(self._APP_VIEW_REQUEST))
        obj = js.get("returnObj", js)
        if isinstance(obj, dict):
            for key in ("Applications", "Apps", "value"):
                if isinstance(obj.get(key), list):
                    return obj[key]
        return obj if isinstance(obj, list) else []

    def export_app(self, view_id: str) -> dict[str, str]:
        """ExportApp Files dict for one app view id (cached to disk)."""
        cached = self._cached_export(view_id)
        if cached is not None:
            return cached
        js = self._post("Ice.LIB.MetaFXSvc/ExportApp", {"viewId": view_id})
        files = js.get("returnObj", {}).get("Files", {}) or {}
        self._store_export(view_id, files)
        if self._throttle:
            time.sleep(self._throttle)
        return files

    def _cache_path(self, view_id: str) -> Path | None:
        if not self._cache_dir:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", view_id)
        return self._cache_dir / f"{safe}.json"

    def _cached_export(self, view_id: str) -> dict[str, str] | None:
        p = self._cache_path(view_id)
        if p and p.exists():
            try:
                return json.loads(p.read_text())
            except (ValueError, OSError):
                return None
        return None

    def _store_export(self, view_id: str, files: dict[str, str]) -> None:
        p = self._cache_path(view_id)
        if p:
            try:
                p.write_text(json.dumps(files))
            except OSError:
                logger.warning("could not cache export for %s", view_id)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

class MenuMapBuilder:
    """Turns live menus + an app registry into the menu->service structural map.

    Parameters
    ----------
    overrides:
        The parsed ``menu_bo_overrides.json`` (``by_menu_id`` / ``by_program`` /
        ``exclude_services`` / ``baseline_services``).
    service_exists:
        Predicate ``(service_id) -> bool`` (usually ``ServiceIndex.service_exists``)
        used to drop hallucinated/absent services from the map.
    """

    def __init__(
        self,
        *,
        overrides: dict | None = None,
        service_exists: Callable[[str], bool] = lambda _s: True,
        known_services: Iterable[str] | None = None,
    ) -> None:
        overrides = overrides or {}
        self._by_menu_id: dict[str, list[str]] = overrides.get("by_menu_id", {}) or {}
        self._by_program: dict[str, list[str]] = overrides.get("by_program", {}) or {}
        self._service_exists = service_exists

        # Canonical map (case/suffix-insensitive key -> index-cased id).  When the
        # full service list is supplied, _resolve_service recovers miscased refs
        # (ERp.BO.Partsvc, Erp.BO.PartsvcSvc) to their true index casing instead
        # of dropping them; without it, falls back to normalize + service_exists.
        self._canon: dict[str, str] = {}
        if known_services is not None:
            for s in known_services:
                k = _canonical_key(s)
                if k:
                    self._canon.setdefault(k, s)

        self._exclude: set[str] = set()
        for s in overrides.get("exclude_services", []):
            resolved = self._resolve_service(s)
            self._exclude.add(resolved or normalize_service(s))
        self._baseline: list[tuple[str, str]] = [
            (self._resolve_service(s if isinstance(s, str) else s.get("service_id", ""))
             or normalize_service(s if isinstance(s, str) else s.get("service_id", "")),
             "" if isinstance(s, str) else s.get("justification", ""))
            for s in overrides.get("baseline_services", [])
        ]

        # Populated by build(): app stem (lower) -> app view id.
        self._stem_to_app: dict[str, str] = {}
        self.report: dict = {}

    # -- app registry ------------------------------------------------------- #

    def index_applications(self, apps: Sequence[dict]) -> None:
        """Build the dll-stem -> app-view-id lookup from the MetaFX registry."""
        self._stem_to_app = {}
        for app in apps:
            view_id = (
                app.get("ViewId") or app.get("viewId") or app.get("Id")
                or app.get("AppId") or app.get("id") or ""
            )
            for name_key in ("Name", "AppName", "Id", "ViewId"):
                name = app.get(name_key)
                if not name:
                    continue
                stem = program_stem(str(name)).lower()
                if stem and view_id:
                    self._stem_to_app.setdefault(stem, str(view_id))

    # -- per-menu strategy chain -------------------------------------------- #

    def map_menu(
        self, menu: dict, export_fn: Callable[[str], dict[str, str]]
    ) -> tuple[set[str], str, float, str]:
        """Resolve one menu to ``(services, strategy, confidence, app_id)``.

        ``export_fn(view_id)`` returns the ExportApp Files dict for an app;
        injected so the chain is testable without a live client.
        """
        menu_id = menu.get("MenuID", "")
        program = menu.get("Program") or menu.get("ProgramKinetic") or ""

        # 1) explicit override by menu id.
        if menu_id in self._by_menu_id:
            return self._finalize(self._by_menu_id[menu_id]), "override", 1.0, ""
        # 2) explicit override by program.
        if program in self._by_program:
            return self._finalize(self._by_program[program]), "override", 1.0, ""
        # 3) dashboard.
        if is_dashboard_program(program):
            return self._finalize(["Ice.BO.DashBoardSvc"]), "dashboard", 0.9, ""
        # 4) metafx export.
        stem = program_stem(program).lower()
        app_id = self._stem_to_app.get(stem, "")
        if app_id:
            try:
                files = export_fn(app_id)
                services = extract_services_from_export(files)
                if services:
                    return self._finalize(services), "metafx", 1.0, app_id
            except Exception as exc:  # export failure -> fall through to heuristic
                logger.warning("ExportApp failed for %s (%s)", app_id, exc)
        # 5) UIProc / Ice.UI.App stem heuristic.
        if stem:
            guess = normalize_service(f"Erp.BO.{program_stem(program)}Svc")
            if self._service_exists(guess):
                return self._finalize([guess]), "heuristic", 0.4, app_id
        # 6) unmapped.
        return set(), "unmapped", 0.0, app_id

    def _resolve_service(self, raw: str) -> str | None:
        """Resolve a raw service ref to its canonical index-cased id, or ``None``.

        Prefers the canonical map (recovers miscased refs); falls back to
        normalize + ``service_exists`` when no service list was supplied.
        """
        if self._canon:
            key = _canonical_key(raw)
            if key is not None and key in self._canon:
                return self._canon[key]
            return None
        svc = normalize_service(raw)
        return svc if (svc and self._service_exists(svc)) else None

    def _finalize(self, services: Iterable[str]) -> set[str]:
        """Resolve to canonical ids, drop excluded + service-index-absent ones."""
        out: set[str] = set()
        for s in services:
            svc = self._resolve_service(s)
            if svc is None:
                self.report.setdefault("dropped_absent_services", set())
                self.report["dropped_absent_services"].add((s or "").strip())
                continue
            if svc in self._exclude:
                continue
            out.add(svc)
        return out

    # -- full build --------------------------------------------------------- #

    def build(
        self,
        menus: Sequence[dict],
        export_fn: Callable[[str], dict[str, str]],
    ) -> tuple[list[dict], list[tuple[str, str, str]], list[tuple[str, int, str]], dict]:
        """Map every launchable menu.

        Returns ``(menu_records, menu_service_rows, unmapped_program_rows,
        report)`` ready to hand to :func:`write_menu_map_db`.
        """
        menu_records: list[dict] = []
        menu_service_rows: list[tuple[str, str, str]] = []
        unmapped: dict[str, list[str]] = {}
        launchable = 0
        mapped = 0

        for menu in menus:
            menu_id = menu.get("MenuID", "")
            program = menu.get("Program") or menu.get("ProgramKinetic") or ""
            enabled = bool(menu.get("MenuEnabled", True))
            is_launchable = enabled and bool(program.strip())

            services: set[str] = set()
            strategy, confidence, app_id = "unmapped", 0.0, ""
            if is_launchable:
                launchable += 1
                services, strategy, confidence, app_id = self.map_menu(menu, export_fn)
                if services:
                    mapped += 1
                else:
                    unmapped.setdefault(program, []).append(menu_id)

            menu_records.append(
                {
                    "menu_id": menu_id,
                    "parent_menu_id": menu.get("ParentMenuID", "") or "",
                    "menu_desc": menu.get("MenuDesc", "") or "",
                    "sec_code": (menu.get("SecCode") or "").strip(),
                    "program": program,
                    "app_id": app_id,
                    "enabled": enabled,
                    "hidden": bool(menu.get("Hidden", False)),
                    "map_strategy": strategy,
                    "map_confidence": confidence,
                }
            )
            for svc in services:
                menu_service_rows.append((menu_id, svc, strategy))

        unmapped_rows = [
            (program, len(ids), ",".join(ids[:10]))
            for program, ids in sorted(unmapped.items())
        ]
        coverage = (mapped / launchable) if launchable else 0.0
        report = {
            "launchable_menus": launchable,
            "mapped_menus": mapped,
            "coverage_pct": round(coverage, 4),
            "unmapped_programs": len(unmapped_rows),
            "strategy_counts": _count_strategies(menu_records),
            "dropped_absent_services": sorted(self.report.get("dropped_absent_services", set())),
        }
        self.report = report
        return menu_records, menu_service_rows, unmapped_rows, report

    def baseline_rows(self) -> list[tuple[str, str]]:
        return [(s, j) for (s, j) in self._baseline if s]


def _count_strategies(menu_records: Sequence[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in menu_records:
        counts[m["map_strategy"]] = counts.get(m["map_strategy"], 0) + 1
    return counts
