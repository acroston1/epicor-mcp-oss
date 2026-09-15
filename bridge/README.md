# Desktop connector

The bridge relays MCP JSON-RPC over stdin/stdout to your hosted `/mcp` endpoint.
It contains no Epicor password or API key. Configure it at runtime:

| Variable | Meaning |
|---|---|
| `MCP_SERVER_URL` | Required full URL, e.g. `https://mcp.example.com/mcp` |
| `MCP_AUTH_MODE` | `none` (default), `token`, or `oauth` |
| `MCP_SERVER_TOKEN` | Required with `token`; matches the server's `EPICOR_MCP_SERVER_TOKEN` |
| `MCP_CA_BUNDLE` | Optional path to a PEM CA certificate bundle for private TLS |

`none` does no OAuth discovery and never opens a sign-in browser. `token` also
avoids Microsoft; it sends the shared bearer token. Remote URLs require HTTPS;
loopback HTTP is accepted for local testing. TLS verification stays enabled.
OAuth is opt-in and caches credentials separately for each server URL.

## Run from Python

From this directory, install `mcp==1.26.0` and `httpx==0.28.1`. For example:

```bash
MCP_SERVER_URL=http://localhost:8015/mcp python epicor_mcp_bridge.py --check
MCP_SERVER_URL=http://localhost:8015/mcp python epicor_mcp_bridge.py
```

`--check` validates configuration and required imports without connecting.
`--version` works without configuration. Protocol messages use stdout; diagnostic
logging uses stderr.

## Build a Windows executable

Use Windows with Python 3.11 or newer and a virtual environment:

```powershell
cd bridge
python -m venv .buildvenv
.\.buildvenv\Scripts\Activate.ps1
.\build_exe.ps1
$env:MCP_SERVER_URL = "https://mcp.example.com/mcp"
$env:MCP_AUTH_MODE = "none"
.\dist\epicor_mcp_oss_bridge.exe --check
```

`build_exe.bat` is the Command Prompt alternative. Both install the dependencies
from `requirements.txt` and package the required MCP submodules and CA data.
Output: `bridge/dist/epicor_mcp_oss_bridge.exe`. Build on Windows: PyInstaller's
output is specific to the build operating system. See the official
[PyInstaller usage guide](https://pyinstaller.org/en/stable/usage.html).
`build_exe.sh` produces a native Linux/macOS executable on those systems.

Copy the executable to the client machine. It does not need Python installed.
Keep LICENSE and NOTICE with redistributed packages. A URL change is a client
configuration change; it does not require a rebuild.

## Client configuration

For a desktop MCP client that accepts `mcpServers`, add:

```json
{
  "mcpServers": {
    "epicor": {
      "command": "C:\\Tools\\EpicorMCP\\epicor_mcp_oss_bridge.exe",
      "env": {
        "MCP_SERVER_URL": "https://mcp.example.com/mcp",
        "MCP_AUTH_MODE": "token",
        "MCP_SERVER_TOKEN": "YOUR_SHARED_SERVER_TOKEN"
      }
    }
  }
}
```

Use `MCP_AUTH_MODE=none` and omit `MCP_SERVER_TOKEN` if the hosted server does not
require a shared token. Use `oauth` only after configuring the server's optional
Microsoft SSO. Restart the client after editing its configuration. If it cannot
connect, run the same executable with `--check` in a terminal and inspect the
client's MCP stderr log. A successful check verifies packaging/configuration;
a successful tool call verifies connectivity and access.
