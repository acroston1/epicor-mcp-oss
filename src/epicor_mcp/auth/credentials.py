"""Epicor credentials from explicit settings and optional administrator-owned files.

No file in a user's home directory is ever opened automatically.
"""
from __future__ import annotations

import base64
import configparser
import json
from pathlib import Path
from typing import Any
from epicor_mcp.config import Settings


class CredentialManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._department_keys: dict[str, dict[str, str]] = {}
        self._admin_keys: dict[str, str] = {}
        self._basic_token = ""
        self._service_username = ""
        self._service_password = ""
        self._ini: dict[str, str] = {}

    def load(self) -> None:
        if self._settings.credentials_path:
            source = Path(self._settings.credentials_path).expanduser()
            parser = configparser.ConfigParser(interpolation=None)
            with source.open(encoding="utf-8") as stream:
                parser.read_file(stream)
            self._ini = dict(parser["epicor"]) if parser.has_section("epicor") else {}
        keys_path = Path(self._settings.department_keys_path)
        if keys_path.exists():
            raw = json.loads(keys_path.read_text(encoding="utf-8"))
            self._admin_keys = raw.get("admin_keys", {})
            self._department_keys = raw.get("departments", {})
        self._service_username, self._service_password = self._load_service_account()
        if self._service_username and self._service_password:
            self._basic_token = base64.b64encode(
                f"{self._service_username}:{self._service_password}".encode()
            ).decode()

    def _load_service_account(self) -> tuple[str, str]:
        return (self._settings.epicor_username or self._ini.get("username", ""),
                self._settings.epicor_password or self._ini.get("password", ""))

    @property
    def service_username(self) -> str:
        return self._service_username

    @property
    def service_password(self) -> str:
        return self._service_password

    @property
    def departments(self) -> list[str]:
        return list(self._department_keys)

    def get_admin_key(self) -> str | None:
        return (self._settings.epicor_api_key or self._ini.get("api_key")
                or self._admin_keys.get("read_key"))

    def get_baq_key(self) -> str | None:
        return (self._settings.epicor_baq_api_key or self._ini.get("baq_api_key")
                or self._admin_keys.get("baq_key") or self.get_admin_key())

    def get_api_key(self, department: str, *, baq: bool = False) -> str:
        keys = self._department_keys.get(department, {})
        value = keys.get("baq_key" if baq else "read_key")
        value = value or (self.get_baq_key() if baq else self.get_admin_key())
        if not value:
            raise KeyError("No Epicor API key configured; set EPICOR_MCP_EPICOR_API_KEY and EPICOR_MCP_EPICOR_BAQ_API_KEY")
        return value

    def build_headers(self, department: str, *, baq: bool = False,
                      environment: str | None = None) -> dict[str, str]:
        if not self._basic_token:
            raise RuntimeError("Set EPICOR_MCP_EPICOR_USERNAME and EPICOR_MCP_EPICOR_PASSWORD")
        return {"Authorization": f"Basic {self._basic_token}",
                "X-API-Key": self.get_api_key(department, baq=baq),
                "Content-Type": "application/json", "Company": self._settings.epicor_company_id}

    def get_base_url(self, environment: str | None = None) -> str:
        env = environment or self._settings.environment
        if env not in {"live", "pilot"}:
            raise ValueError(f"Unknown Epicor environment: {env!r}")
        return (self._settings.epicor_live_url if env == "live"
                else self._settings.epicor_pilot_url).rstrip("/")


def create_default_department_keys_file(path: Path) -> None:
    """Write a placeholder template only when explicitly requested by an operator."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"admin_keys": {}, "departments": {}, "environments": {}}, indent=2) + "\n")
