# bridge/

Purpose: a standalone stdio-to-Streamable-HTTP connector for desktop MCP clients, built
into a native executable with PyInstaller. Operator docs: README.md in this directory.

## Public interface
- `epicor_mcp_bridge.py`: `MCP_SERVER_URL` (required), `MCP_AUTH_MODE` = `none` |
  `token` | `oauth`, `MCP_SERVER_TOKEN`, `MCP_CA_BUNDLE`; flags `--check`, `--version`.
- `build_exe.ps1` / `build_exe.bat` (Windows), `build_exe.sh` (Linux/macOS): produce
  `dist/epicor_mcp_oss_bridge[.exe]`. `requirements.txt` pins `mcp` and `httpx`.

## Invariants
- The executable contains no server URL, Epicor password or API key; everything is
  runtime environment. A URL change never needs a rebuild.
- `none` performs no OAuth discovery and never opens a browser; `token` sends
  `Authorization: Bearer` and also avoids Microsoft. `oauth` is opt-in and caches
  credentials per server URL.
- Remote URLs must be `https://`; loopback `http://` is accepted for local testing.
  TLS verification is never disabled; `MCP_CA_BUNDLE` adds a private CA.
- Protocol messages use stdout only; diagnostics go to stderr.
- Tool arguments are relayed unchanged; they never trigger local file reads.
- PyInstaller output is OS-specific: build the Windows executable on Windows.

## Gotchas
- `--check` validates configuration and bundled imports without connecting; a passing
  check proves packaging, not server access.
- Hidden imports (`mcp.client.streamable_http`, `mcp.shared.message`, `mcp.types`) and
  `certifi` data are required; keep them in all three build scripts together.
- `tests/test_oss_bridge.py` imports the script as a module; keep it importable
  without side effects.
