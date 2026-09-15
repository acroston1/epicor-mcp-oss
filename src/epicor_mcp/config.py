"""Portable settings, loaded from .env in the working directory or EPICOR_MCP_* env."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EPICOR_MCP_", env_file=".env",
        env_file_encoding="utf-8", extra="ignore")

    auth_mode: Literal["none", "azure_ad"] = "none"
    server_token: str = Field(default="", repr=False)
    table_whitelist_path: str = "table_whitelist.txt"
    # Raw input avoids settings-source JSON decoding: blank is allowed, and
    # the validator parses exactly once and always stores a dict[str, str].
    plants: Any = Field(default_factory=dict)
    azure_admin_emails: str = ""
    azure_cloud: Literal["commercial", "government"] = "commercial"
    epicor_api_key: str = Field(default="", repr=False)
    epicor_baq_api_key: str = Field(default="", repr=False)
    credentials_path: str = ""
    service_index_path: Path = Path("data/service_index.db")
    baq_schema_path: Path = Path("data/baq_schema.db")
    docs_db_path: Path = Path("data/epicor_docs.db")
    document_vectors_path: Path = Path("data/document_vectors")
    help_store_enabled: bool = False

    azure_tenant_id: str = ''
    azure_client_id: str = ''
    azure_client_secret: str = Field(default='', repr=False)
    epicor_username: str = ''
    epicor_password: str = Field(default='', repr=False)
    epicor_pilot_url: str = ''
    epicor_live_url: str = ''
    epicor_company_id: str = ''
    port: int = 8015
    host: str = '127.0.0.1'
    environment: Literal['pilot', 'live'] = 'live'
    users_config_path: Path = Path('data/users.json')
    department_keys_path: Path = Path('data/department_keys.json')
    dev_mode: bool = False
    allow_dev_mode_query_tool: bool = False
    public_surface: bool = True
    admin_secret: str = Field(default='', repr=False)
    vector_store_path: Path = Path('data/vectors')
    embedding_model: str = ''
    vector_search_enabled: bool = False
    help_store_path: Path = Path('data/help_store')
    embed_endpoint: str = ''
    embed_model_name: str = ''
    embed_dim: int = 2048
    reranker_model: str = ''
    embed_sleep_after_s: int = 0
    reranker_unload_after_s: int = 0
    forum_live_enabled: bool = False
    forum_base_url: str = 'https://www.epiusers.help'
    enable_child_query: bool = False
    enable_baq_create: bool = False
    audit_log_enabled: bool = True
    audit_log_path: Path = Path('data/audit.db')
    attachment_path_map: dict[str, str] = Field(default_factory=dict)
    menu_authz_mode: Literal['off', 'shadow', 'enforce'] = 'off'
    menu_authz_ttl_seconds: int = 300
    menu_authz_stale_grace_seconds: int = 3600
    menu_map_db_path: Path = Path('data/menu_security.db')
    menu_map_overrides_path: Path = Path('data/menu_bo_overrides.json')
    menu_authz_live_url: str = ''
    log_level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO'
    response_max_bytes: int = 100000
    response_offload_enabled: bool = False
    response_offload_dir: Path = Path('data/results')
    response_public_base_url: str = 'http://localhost:8015'
    response_offload_preview_rows: int = 5
    response_offload_retention_hours: int = 24
    response_truncate_keep: int = 10
    response_stats_top_k: int = 10
    response_drop_empty: bool = True
    response_aggressive_strip: bool = True
    response_max_string_chars: int | None = 300
    sql_execute_timeout_s: float = 25.0
    sql_max_inflight: int = 2
    sql_session_budget_s: float = 120.0
    sql_strict_scan_guard: bool = False
    sql_validate_columns: bool = True
    mcp_client_origins: str = 'https://claude.ai,https://cowork.anthropic.com'
    discovery_index_path: Path = Path('data/discovery_index')
    table_authz_mode: str = 'gate'
    dev_identity: str = ''
    table_blacklist_path: Path = Path('table_blacklist.txt')
    table_keys_path: Path = Path("data/table_keys.json")
    sql_ground_domains: bool = False
    sql_diagnose_empty: bool = False
    sql_lint_fanout_warning: bool = False
    sql_domain_cache_ttl_s: float = 8 * 60 * 60
    sql_diagnose_probe_budget: int = 5

    @field_validator("plants", mode="before")
    @classmethod
    def validate_plants(cls, value: Any) -> dict[str, str]:
        message = "EPICOR_MCP_PLANTS must be a JSON object of non-empty site codes and names"
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(message) from exc
        if not isinstance(value, dict) or any(
            not isinstance(code, str) or not code.strip()
            or not isinstance(name, str) or not name.strip()
            for code, name in value.items()
        ):
            raise ValueError(message)
        return dict(value)

    @model_validator(mode="after")
    def validate_modes(self):
        if self.dev_mode or self.allow_dev_mode_query_tool or self.dev_identity:
            raise ValueError("Development identity bypasses are unsupported. Use AUTH_MODE=none with a table whitelist.")
        if not self.public_surface:
            raise ValueError("Only the five-tool public surface is supported; PUBLIC_SURFACE must be true.")
        if self.auth_mode == "none" and self.enable_baq_create:
            raise ValueError("BAQ saving is disabled without Microsoft SSO.")
        return self

    def validate_runtime(self) -> None:
        """Fail at startup with actionable names; parsing settings alone is offline."""
        required = {"EPICOR_USERNAME": self.epicor_username,
                    "EPICOR_PASSWORD": self.epicor_password,
                    "EPICOR_COMPANY_ID": self.epicor_company_id,
                    "EPICOR_BAQ_API_KEY": self.epicor_baq_api_key or self.epicor_api_key}
        if not self.epicor_company_id.strip():
            raise ValueError("Missing required configuration: EPICOR_MCP_EPICOR_COMPANY_ID")
        if not self.credentials_path:
            missing = ["EPICOR_MCP_" + name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError("Missing required configuration: " + ", ".join(missing))
        url = self.epicor_base_url
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or "/api/v2/odata/" not in parsed.path.lower():
            raise ValueError("EPICOR_MCP_EPICOR_" + self.environment.upper() + "_URL must be a full Epicor REST v2 OData company URL.")
        if self.auth_mode == "azure_ad":
            if not self.azure_tenant_id or not self.azure_client_id or not self.azure_client_secret:
                raise ValueError("Azure SSO requires AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (EPICOR_MCP_ prefix).")
            if not self.response_public_base_url.startswith("https://"):
                raise ValueError("Azure SSO requires EPICOR_MCP_RESPONSE_PUBLIC_BASE_URL=https://your-server.example")

    @property
    def epicor_base_url(self) -> str:
        return (self.epicor_live_url if self.environment == "live" else self.epicor_pilot_url).rstrip("/")

    @property
    def azure_host(self) -> str:
        return "login.microsoftonline.us" if self.azure_cloud == "government" else "login.microsoftonline.com"

    @property
    def azure_authority(self) -> str:
        return f"https://{self.azure_host}/{self.azure_tenant_id}"

    @property
    def azure_jwks_url(self) -> str:
        return f"{self.azure_authority}/discovery/v2.0/keys"

    @property
    def azure_issuer(self) -> str:
        return f"{self.azure_authority}/v2.0"

    @property
    def azure_openid_config_url(self) -> str:
        return f"{self.azure_authority}/v2.0/.well-known/openid-configuration"

_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
