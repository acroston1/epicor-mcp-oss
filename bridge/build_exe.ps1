$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
python -m PyInstaller --onefile --name epicor_mcp_oss_bridge `
    --distpath dist --workpath build --specpath . --clean --noconfirm `
    --collect-data certifi --hidden-import mcp.client.streamable_http `
    --hidden-import mcp.shared.message --hidden-import mcp.types epicor_mcp_bridge.py
if ($LASTEXITCODE -ne 0) { throw "Executable build failed" }
Write-Host "Built dist\epicor_mcp_oss_bridge.exe"
