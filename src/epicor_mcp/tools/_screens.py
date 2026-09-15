"""Screen-grounded discovery for ``epicor_read`` (menu -> BOs -> entities -> fields).

Users think in *screens* ("I'm on the Job Tracker screen"), and a weak model,
stuck on where data lives, will ask which screen the user is on — then drop the
answer, because nothing consumed it. The optional menu map closes that loop:
``data/menu_security.db`` (built by ``scripts/build_menu_map.py``) links each
Epicor menu/screen to the business-object services behind it (built for RBAC). This module exposes it as
a *discovery* affordance.

The design principle is COLLAPSE, not CHAIN: the map only goes screen -> service, so rather than make the model
walk screen -> BO -> fields -> query as four turns, one call returns the BOs,
their entity sets, AND each entity's key fields already attached — RBAC-filtered
to what the user may actually query — so the model reads the answer in one shot.

Two entry points:
  * ``detect_screen_query`` / ``screen_discovery`` — the model (or the user's
    system prompt) named a screen; hand back its tables and fields.
  * ``screens_for_term`` — a data phrase failed to resolve; enrich the INV-1
    envelope with any screens whose name matches, so a named-screen retry lands.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from epicor_mcp.tools._inline_schema import FIELD_OVERRIDES, TOP_SERVICES
from epicor_mcp.tools._resolve import error_envelope, resolve_fields

if TYPE_CHECKING:
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_TOP_ENTITIES = {svc: ents for svc, ents in TOP_SERVICES}
_MAX_ENTITIES_PER_SVC = 4
_MAX_FIELDS_PER_ENTITY = 10
_MAX_SCREEN_SIGNATURES = 8


def _default_db_path() -> Path:
    # _screens.py -> tools -> epicor_mcp -> src -> <repo root>/data/menu_security.db
    return Path(__file__).resolve().parents[3] / "data" / "menu_security.db"


class ScreenMap:
    """Read-only reader of the screen -> service map, with mtime hot-reload.

    Loads ``menus`` (menu_id -> caption/program) and ``menu_services`` (menu_id
    -> services). Fail-open to an empty map: a missing/unreadable db just means
    screen discovery finds nothing, never a crash.
    """

    def __init__(self, db_path: "str | Path | None" = None) -> None:
        self._path = Path(db_path) if db_path else _default_db_path()
        self._mtime: float | None = None
        self._menu_desc: dict[str, str] = {}
        self._menu_program: dict[str, str] = {}
        self._menu_services: dict[str, set[str]] = {}
        self._loaded = False
        self._load()

    def _load(self) -> bool:
        self._menu_desc = {}
        self._menu_program = {}
        self._menu_services = {}
        self._loaded = False
        self._mtime = None
        if not self._path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            try:
                for menu_id, desc, program in conn.execute(
                    "SELECT menu_id, menu_desc, program FROM menus"
                ):
                    if desc:
                        self._menu_desc[menu_id] = desc
                    if program:
                        self._menu_program[menu_id] = program
                for menu_id, service_id in conn.execute(
                    "SELECT menu_id, service_id FROM menu_services"
                ):
                    self._menu_services.setdefault(menu_id, set()).add(service_id)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("menu_security.db unreadable for screen map (%s)", exc)
            return False
        self._loaded = True
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None
        return True

    def maybe_reload(self) -> None:
        """Reload on a nightly rebuild (mtime advance) — mirrors MenuMapStore."""
        try:
            if not self._path.exists():
                return
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if self._mtime is None or mtime > self._mtime:
            self._load()

    def is_loaded(self) -> bool:
        return self._loaded

    def screens_matching(self, name: str, *, limit: int = _MAX_SCREEN_SIGNATURES) -> list:
        """Screens whose caption matches *name*, collapsed by (caption, services).

        Match = every word of the query appears in the caption (case-insensitive)
        as a whole word or substring. The same caption ("Job Tracker") appears
        under dozens of menu ids with identical services, so results are
        deduped to distinct (caption, service-set) signatures.
        """
        words = [w for w in re.split(r"\s+", (name or "").strip().lower()) if w]
        if not words:
            return []
        sigs: dict[tuple, dict] = {}
        for menu_id, desc in self._menu_desc.items():
            dl = desc.lower()
            if not all(w in dl for w in words):
                continue
            services = tuple(sorted(self._menu_services.get(menu_id, set())))
            if not services:
                continue
            key = (desc.lower(), services)
            entry = sigs.setdefault(key, {
                "caption": desc,
                "services": list(services),
                "menu_ids": [],
                "program": self._menu_program.get(menu_id, ""),
            })
            entry["menu_ids"].append(menu_id)
        # Prefer an exact caption match, then more menu ids (more central), then
        # fewer services (a tighter, more specific screen).
        nl = (name or "").strip().lower()
        ranked = sorted(
            sigs.values(),
            key=lambda e: (e["caption"].lower() != nl,
                           -len(e["menu_ids"]), len(e["services"])),
        )
        return ranked[:limit]


def _clean_screen_name(raw: str) -> str:
    """Trim a captured screen name; blank it if it looks like a data clause."""
    t = (raw or "").strip().strip("?.!,").strip()
    t = re.sub(r"^(?:the|my|this|a|an|epicor)\s+", "", t, flags=re.I).strip()
    # A filter/data expression is not a screen name.
    if re.search(r"[=<>']|\b(?:eq|ne|like|and|or)\b", t, re.I):
        return ""
    # Drop a trailing screen-type noun the caller may have left on.
    t = re.sub(r"\s+(?:screen|form|window|tab|app|application)s?$", "", t, flags=re.I)
    return t.strip()


_LIST_RE = re.compile(
    r"\b(?:what|which|list)\s+(?:epicor\s+)?screens?\b", re.I)
_DATA_WORDS = r"(?:tables?|bo['s]?|bos|business\s+objects?|fields?|data|services?)"
_BEHIND_RE = re.compile(
    _DATA_WORDS + r"\s+(?:are\s+|is\s+|that\s+)?"
    r"(?:behind|for|in|on|under|of|used\s+by)\s+(.+)", re.I)
_WHATS_BEHIND_RE = re.compile(r"\bwhat(?:'s| is|s)?\s+behind\s+(.+)", re.I)
# "<X> screen/form/window/tab" — trailing screen-type noun only (NOT
# entry/tracker/maintenance, which collide with real data phrasings).
_TRAIL_RE = re.compile(r"^(.*?\S)\s+(?:screen|form|window|tab)s?\s*$", re.I)


def detect_screen_query(target: str) -> "tuple[str, str] | None":
    """Classify *target* as a screen-discovery ask.

    Returns ``("list", "")`` for "which screens can I see", ``("lookup",
    name)`` for "tables behind the Job Tracker screen" / "Job Tracker screen",
    or ``None`` for an ordinary data read.
    """
    t = (target or "").strip()
    if not t:
        return None
    if _LIST_RE.search(t) and not re.search(r"\b(?:behind|for|of|on)\b", t, re.I):
        return ("list", "")
    for rx in (_BEHIND_RE, _WHATS_BEHIND_RE):
        m = rx.search(t)
        if m:
            name = _clean_screen_name(m.group(1))
            if name:
                return ("lookup", name)
    if re.search(r"\b(?:screen|form|window|tab)s?\b", t, re.I):
        m = _TRAIL_RE.match(t)
        if m:
            name = _clean_screen_name(m.group(1))
            if name:
                return ("lookup", name)
    return None


def _entities_for_service(index: "ServiceIndex", service: str) -> list:
    """Curated entity sets for *service*, or the first real tables from the index."""
    ents = _TOP_ENTITIES.get(service)
    if not ents:
        try:
            ents = [
                es for es in (index.get_entity_sets(service) or [])
                if index.get_fields(service, es)
            ]
        except Exception:
            ents = []
    return list(ents)[:_MAX_ENTITIES_PER_SVC]


def _key_fields(index: "ServiceIndex", service: str, entity: str) -> list:
    """Curated key fields for service/entity — the columns the model should use."""
    curated = FIELD_OVERRIDES.get((service, entity))
    if curated:
        try:
            valid = {f["field_name"].lower()
                     for f in (index.get_fields(service, entity) or [])
                     if f.get("field_name")}
        except Exception:
            valid = set()
        if valid:
            kept = [c for c in curated if c.lower() in valid]
            if kept:
                return kept[:_MAX_FIELDS_PER_ENTITY]
        return list(curated)[:_MAX_FIELDS_PER_ENTITY]
    fields = resolve_fields(index, service, entity, "")["fields"]
    if fields:
        return fields[:_MAX_FIELDS_PER_ENTITY]
    try:
        return [f["field_name"] for f in (index.get_fields(service, entity) or [])
                if f.get("field_name")][:_MAX_FIELDS_PER_ENTITY]
    except Exception:
        return []


def screen_discovery(
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    session,
    screen_map: ScreenMap,
    *,
    mode: str,
    name: str,
) -> str:
    """Answer a screen-discovery ask with the BOs/entities/fields behind it."""
    if mode == "list" or not name:
        return _dumps(error_envelope(
            "name_a_screen",
            "Name the Epicor screen you're on (e.g. \"Job Tracker screen\" or "
            "\"tables behind the Job Tracker screen\") and I'll list the "
            "business objects, tables, and key fields behind it that you can "
            "query. I can't enumerate every screen, but I can map any one you "
            "name.",
        ))

    screens = screen_map.screens_matching(name)
    if not screens:
        return _dumps(error_envelope(
            "screen_not_found",
            f"No Epicor screen matches '{name}'. Check the screen caption "
            "(its title-bar text) and try again, or just describe the data "
            "you want and I'll find the table.",
        ))

    # Union the services behind the matched screen(s), RBAC-filter, attach
    # entities + key fields.
    services: list[str] = []
    for s in screens:
        for svc in s["services"]:
            if svc not in services:
                services.append(svc)

    granted: list[dict] = []
    hidden = 0
    for svc in services:
        try:
            allowed, _msg = rbac.check_access(session.user_id, svc)
        except Exception:
            allowed = False
        if not allowed:
            hidden += 1
            continue
        entities = []
        for ent in _entities_for_service(index, svc):
            entities.append({
                "entity_set": ent,
                "key_fields": _key_fields(index, svc, ent),
                "target": f"{svc}/{ent}",
            })
        if entities:
            granted.append({"service": svc, "entities": entities})

    matched_caption = screens[0]["caption"]
    if not granted:
        return _dumps(error_envelope(
            "screen_no_access",
            f"Screen '{matched_caption}' is served by "
            f"{len(services)} business object(s), but you don't have access to "
            "any of them. Contact your administrator if you need this data.",
            valid={"screen": matched_caption, "services": services},
        ))

    best = granted[0]["entities"][0]["target"]
    out = {
        "summary": (f"Screen '{matched_caption}' → {len(granted)} business "
                    f"object(s) you can query"
                    + (f" ({hidden} more you can't access are hidden)"
                       if hidden else "") + "."),
        "stop_hint": ("These ARE the tables behind that screen — pick the "
                      "entity + fields you need and call epicor_read(target="
                      f"\"{best}\", fields=..., where=...). Do NOT hunt "
                      "epicor_help or guess a business object."),
        "screen": {
            "matched": [s["caption"] for s in screens[:3]],
            "menu_ids": screens[0]["menu_ids"][:5],
        },
        "business_objects": granted,
        "retry_with": {"target": best},
    }
    if hidden:
        out["hidden_services"] = hidden
    return _dumps(out)


def screens_for_term(screen_map: ScreenMap, term: str) -> list:
    """Screens whose caption matches a (failed) data phrase — envelope enrichment.

    Used to enrich an unresolved-target envelope: if the phrase names a screen,
    hand back that screen's services so a retry can land. Returns a compact
    list of ``{screen, services}`` (empty when nothing matches).
    """
    if not term or not screen_map.is_loaded():
        return []
    hits = screen_map.screens_matching(term, limit=3)
    return [{"screen": h["caption"], "services": h["services"]} for h in hits]


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str)
