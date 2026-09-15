#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --name epicor_mcp_oss_bridge \
  --distpath dist --workpath build --specpath . --clean --noconfirm \
  --collect-data certifi --hidden-import mcp.client.streamable_http \
  --hidden-import mcp.shared.message --hidden-import mcp.types epicor_mcp_bridge.py
echo "Built dist/epicor_mcp_oss_bridge for this operating system (not a Windows cross-build)."
