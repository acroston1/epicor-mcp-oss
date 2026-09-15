"""Empty compatibility hints for private legacy tools; public hints use Settings."""
from __future__ import annotations
import re


# Legacy helpers bind this at import. Never populate it from an app or the
# environment: simultaneous server instances must not share installation data.
PLANTS: dict[str, str] = {}
PART_BRAND_FIELD = "CommercialBrand"


def plant_lines() -> str:
    return " | ".join(f"{code} {name}" for code, name in PLANTS.items()) or "No site map configured; look up site codes in Epicor."


def match_plant(text: str) -> str | None:
    for code, name in sorted(PLANTS.items(), key=lambda item: -len(item[1])):
        if re.search(rf"\b{re.escape(name)}\b", text or "", re.I):
            return code
    return None


def commercial_brand_lines() -> str:
    return "Customer attribution depends on your installation. Do not infer it from part numbers or assume CommercialBrand is a customer key."
