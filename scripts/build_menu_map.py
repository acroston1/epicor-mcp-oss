#!/usr/bin/env python3
"""Optional Microsoft SSO menu map import using configured Epicor read access.

Run only when setting up menu-derived authorization. Reads menu/application
metadata and writes local indexes; never changes Epicor records. Requires an
API key scoped for Ice.LIB.MetaFXSvc and the menu services.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from epicor_mcp.index.service_index import ServiceIndex
from epicor_mcp.rbac.menu_map_builder import (
    EpicorMetaClient,
    MenuMapBuilder,
    write_menu_map_db,
)

logger = logging.getLogger("build_menu_map")

_DEFAULT_OUTPUT = _REPO_ROOT / "data" / "menu_security.db"
_DEFAULT_OVERRIDES = _REPO_ROOT / "data" / "menu_bo_overrides.json"
_DEFAULT_CACHE = _REPO_ROOT / "data" / "metafx_cache"
_DEFAULT_REPORT = _REPO_ROOT / "data" / "menu_map_report.json"
_DEFAULT_SERVICE_INDEX = _REPO_ROOT / "data" / "service_index.db"


def _load_creds() -> dict:
    from epicor_mcp.auth.credentials import CredentialManager
    from epicor_mcp.config import Settings
    settings = Settings()
    credentials = CredentialManager(settings)
    credentials.load()
    if not credentials.service_username or not credentials.service_password or not credentials.get_admin_key():
        raise SystemExit("Configure Epicor service credentials and an API key with menu/MetaFX read access")
    return {"username": credentials.service_username, "password": credentials.service_password,
            "base_url": credentials.get_base_url("live"), "api_key": credentials.get_admin_key()}


def _existing_mapped_count(db_path: Path) -> int:
    """Distinct mapped menus in an existing db (0 if it does not exist)."""
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            (n,) = conn.execute(
                "SELECT COUNT(DISTINCT menu_id) FROM menu_services"
            ).fetchone()
            return int(n)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT))
    parser.add_argument("--overrides", default=str(_DEFAULT_OVERRIDES))
    parser.add_argument("--apps-cache", default=str(_DEFAULT_CACHE))
    parser.add_argument("--report", default=str(_DEFAULT_REPORT))
    parser.add_argument("--service-index", default=str(_DEFAULT_SERVICE_INDEX))
    parser.add_argument("--env", default="live", choices=["live"],
                        help="Authorization reads are LIVE-only (pilot security is stale).")
    parser.add_argument("--refresh-all", action="store_true",
                        help="Ignore the ExportApp disk cache and re-export every app.")
    parser.add_argument("--min-coverage", type=float, default=0.80,
                        help="Refuse to replace the db below this launchable-menu coverage.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Smoke mode: export at most N distinct apps (0 = no limit).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the coverage / regression safety valve.")
    args = parser.parse_args()

    output = Path(args.output)
    creds = _load_creds()
    base_url = creds["base_url"].rstrip("/") + "/"

    overrides = {}
    if Path(args.overrides).exists():
        overrides = json.loads(Path(args.overrides).read_text())

    index = ServiceIndex(args.service_index)
    # Full canonical service list lets the builder recover miscased refs
    # (ERp.BO.Partsvc, Erp.BO.PartsvcSvc) to their true index casing rather than
    # dropping them; true garbage (%svc%, non-BO strings) still drops.
    conn = sqlite3.connect(f"file:{Path(args.service_index)}?mode=ro", uri=True)
    try:
        known_services = [r[0] for r in conn.execute("SELECT service_id FROM services")]
    finally:
        conn.close()
    builder = MenuMapBuilder(
        overrides=overrides,
        service_exists=index.service_exists,
        known_services=known_services,
    )

    if args.refresh_all:
        import shutil
        shutil.rmtree(args.apps_cache, ignore_errors=True)

    with EpicorMetaClient(
        base_url, creds["username"], creds["password"], creds["api_key"],
        cache_dir=args.apps_cache,
    ) as client:
        logger.info("fetching menus (LIVE)...")
        menus = client.fetch_menus()
        logger.info("  %d menu rows", len(menus))
        logger.info("fetching MetaFX app registry...")
        apps = client.get_applications()
        logger.info("  %d apps", len(apps))
        builder.index_applications(apps)

        # Smoke limit: cap distinct ExportApp calls.
        exported: set[str] = set()

        def export_fn(view_id: str):
            if args.limit and view_id not in exported and len(exported) >= args.limit:
                return {}
            exported.add(view_id)
            return client.export_app(view_id)

        logger.info("mapping menus through the strategy chain...")
        menu_records, menu_service_rows, unmapped_rows, report = builder.build(
            menus, export_fn
        )

    baseline_rows = builder.baseline_rows()
    coverage = report["coverage_pct"]
    new_mapped = report["mapped_menus"]
    prev_mapped = _existing_mapped_count(output)

    # ---- safety valve ---------------------------------------------------- #
    refuse = None
    if not args.limit:  # smoke runs never gate
        if coverage < args.min_coverage:
            refuse = (f"coverage {coverage:.1%} < --min-coverage "
                      f"{args.min_coverage:.0%}")
        elif prev_mapped and new_mapped < prev_mapped * 0.90:
            refuse = (f"mapped-menu regression: {new_mapped} < 90% of previous "
                      f"{prev_mapped}")

    report.update({
        "base_url": base_url,
        "previous_mapped_menus": prev_mapped,
        "baseline_services": [s for s, _ in baseline_rows],
        "output": str(output),
        "limit": args.limit,
        "refused": refuse,
    })
    Path(args.report).write_text(json.dumps(report, indent=2, default=list))

    print("\n=== Menu Map Build ===")
    print(f"  Menus:            {len(menu_records):,}")
    print(f"  Launchable:       {report['launchable_menus']:,}")
    print(f"  Mapped:           {new_mapped:,}  (prev {prev_mapped:,})")
    print(f"  Coverage:         {coverage:.1%}")
    print(f"  Strategy counts:  {report['strategy_counts']}")
    print(f"  Unmapped progs:   {report['unmapped_programs']:,}")
    print(f"  Dropped services: {len(report['dropped_absent_services'])}")
    print(f"  Report:           {args.report}")

    if refuse and not args.force:
        print(f"\nREFUSED to replace {output}: {refuse}")
        print("  Review the report; re-run with --force to override.")
        sys.exit(2)

    if args.limit:
        print(f"\nSMOKE run (--limit {args.limit}) — db NOT written.")
        return

    write_menu_map_db(
        output,
        menus=menu_records,
        menu_services=menu_service_rows,
        baseline_services=baseline_rows,
        unmapped_programs=unmapped_rows,
        meta={
            "built_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "coverage_pct": coverage,
            "launchable_menus": report["launchable_menus"],
            "mapped_menus": new_mapped,
            "strategy_counts": json.dumps(report["strategy_counts"]),
            "base_url": base_url,
        },
    )
    print(f"\nWrote {output}")


if __name__ == "__main__":
    main()
