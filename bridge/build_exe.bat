@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m PyInstaller --onefile --name epicor_mcp_oss_bridge --distpath dist --workpath build --specpath . --clean --noconfirm --collect-data certifi --hidden-import mcp.client.streamable_http --hidden-import mcp.shared.message --hidden-import mcp.types epicor_mcp_bridge.py
if errorlevel 1 exit /b 1
echo Built dist\epicor_mcp_oss_bridge.exe
