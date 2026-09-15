"""Epicor MCP OSS Bridge — standalone stdio-to-HTTP connector.

Connects desktop MCP clients (stdio) to a hosted server (Streamable HTTP)
with optional shared-token or OAuth authentication. Compiles to a standalone .exe via
PyInstaller — no Node.js or Python needed on user machines.

Usage (development):
    python epicor_mcp_bridge.py

Usage (compiled):
    epicor_mcp_oss_bridge.exe

Set MCP_SERVER_URL and optionally MCP_AUTH_MODE/MCP_SERVER_TOKEN in the client environment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ssl
from dataclasses import dataclass, field
import json
import logging
import os
import secrets
import socket
import sys
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")


@dataclass(frozen=True)
class BridgeConfig:
    server_url: str
    auth_mode: str = "none"
    server_token: str = field(default="", repr=False)
    ca_bundle: str = ""


def load_config() -> BridgeConfig:
    """Read client settings; no browser or network activity occurs here."""
    url = os.environ.get("MCP_SERVER_URL", "").strip()
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Set MCP_SERVER_URL to the full http(s) MCP endpoint, without credentials/query/fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote MCP_SERVER_URL must use HTTPS; HTTP is allowed only for loopback testing")
    mode = os.environ.get("MCP_AUTH_MODE", "none").strip().lower()
    if mode not in {"none", "token", "oauth"}:
        raise ValueError("MCP_AUTH_MODE must be none, token, or oauth")
    token = os.environ.get("MCP_SERVER_TOKEN", "").strip()
    if mode == "token" and not token:
        raise ValueError("MCP_SERVER_TOKEN is required for token mode")
    ca = os.environ.get("MCP_CA_BUNDLE", "").strip()
    if ca and not Path(ca).is_file():
        raise ValueError("MCP_CA_BUNDLE must point to a PEM CA certificate file")
    return BridgeConfig(url.rstrip("/"), mode, token, ca)

OAUTH_CALLBACK_PORT_START = 39900  # First port we try for the loopback callback;
OAUTH_CALLBACK_PORT_END = 39999    # if it's busy we scan up to this.
TOKEN_CACHE_FILE = Path.home() / ".epicor_mcp_oss_tokens.json"
TOKEN_CACHE_LOCK_FILE = Path.home() / ".epicor_mcp_oss_tokens.lock"

logger = logging.getLogger("epicor-mcp-oss-bridge")


class _BridgeShutdown(Exception):
    """Raised inside the TaskGroup to trigger a clean bridge exit.

    When stdin closes (the desktop client disconnected) or the server stream
    ends, one of the relay tasks raises this. The TaskGroup cancels the
    other tasks and ``run_bridge`` catches it as a normal shutdown.
    """


# ---------------------------------------------------------------------------
# Cross-platform file locking helpers
# ---------------------------------------------------------------------------

def _flock_exclusive_nb(fp) -> bool:
    """Try to take an exclusive non-blocking lock on `fp`. Returns True on success."""
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _funlock(fp) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


async def _acquire_token_cache_lock(timeout: float = 30.0):
    """Acquire an exclusive cross-process lock on the token cache.

    Returns the open file handle; caller must release with ``_funlock`` and
    then ``close()``. Raises TimeoutError on failure.
    """
    TOKEN_CACHE_LOCK_FILE.touch(exist_ok=True)
    fp = open(TOKEN_CACHE_LOCK_FILE, "r+")
    deadline = time.time() + timeout
    while True:
        if _flock_exclusive_nb(fp):
            return fp
        if time.time() > deadline:
            fp.close()
            raise TimeoutError(f"token cache lock not acquired within {timeout}s")
        await asyncio.sleep(0.1)


def _find_free_oauth_port() -> int:
    """Find a free TCP port on 127.0.0.1 in the configured callback range.

    Each bridge picks its own port at startup so concurrent OAuth flows
    from multiple bridges don't collide on a fixed port.
    """
    for port in range(OAUTH_CALLBACK_PORT_START, OAUTH_CALLBACK_PORT_END + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        sock.close()
        return port
    raise RuntimeError(
        f"No free port in {OAUTH_CALLBACK_PORT_START}-{OAUTH_CALLBACK_PORT_END} "
        "for the OAuth callback server."
    )

# ---------------------------------------------------------------------------
# Token cache (persists across restarts)
# ---------------------------------------------------------------------------

def _load_cached_tokens() -> dict:
    if TOKEN_CACHE_FILE.exists():
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text())
            if isinstance(data, dict) and data.get("access_token"):
                return data
        except Exception:
            pass
    return {}


def _save_cached_tokens(tokens: dict) -> None:
    try:
        TOKEN_CACHE_FILE.write_text(json.dumps(tokens))
        # Restrict permissions to owner-only (tokens contain secrets)
        try:
            TOKEN_CACHE_FILE.chmod(0o600)
        except OSError:
            pass  # Windows doesn't support chmod the same way
    except Exception as exc:
        logger.warning("Failed to cache tokens: %s", exc)


def _clear_cached_tokens() -> None:
    try:
        TOKEN_CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OAuth discovery and registration
# ---------------------------------------------------------------------------

async def discover_oauth(client: httpx.AsyncClient, server_url: str) -> dict:
    """Discover OAuth endpoints from the MCP server."""
    base = server_url.rsplit("/mcp", 1)[0]

    # Discover protected resource
    r = await client.get(f"{base}/.well-known/oauth-protected-resource")
    r.raise_for_status()
    resource_meta = r.json()

    # Discover authorization server
    auth_server = resource_meta.get("authorization_servers", [base])[0]
    r = await client.get(f"{auth_server}/.well-known/oauth-authorization-server")
    r.raise_for_status()
    auth_meta = r.json()

    return {
        "resource": resource_meta.get("resource", server_url),
        "authorization_endpoint": auth_meta["authorization_endpoint"],
        "token_endpoint": auth_meta["token_endpoint"],
        "registration_endpoint": auth_meta.get("registration_endpoint"),
    }


async def register_client(
    client: httpx.AsyncClient,
    registration_endpoint: str,
    callback_port: int,
) -> str:
    """Register as an OAuth client and get a client_id.

    ``callback_port`` is chosen per-bridge so concurrent bridges do not
    collide on a fixed loopback port during dynamic registration.
    """
    r = await client.post(
        registration_endpoint,
        json={
            "client_name": "epicor-mcp-oss-bridge",
            "redirect_uris": [f"http://localhost:{callback_port}/oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    r.raise_for_status()
    return r.json()["client_id"]


# ---------------------------------------------------------------------------
# OAuth callback server (receives the authorization server's redirect)
# ---------------------------------------------------------------------------

class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth callback GET request from the MCP server's /oauth/callback redirect."""

    code: str | None = None
    state: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        _OAuthCallbackHandler.code = params.get("code", [None])[0]
        _OAuthCallbackHandler.state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h2>Authenticated successfully.</h2>"
                         b"<p>You can close this tab and return to your MCP client.</p>"
                         b"<script>setTimeout(function(){window.close()},2000);</script>"
                         b"</body></html>")

    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs


def _run_callback_server(callback_port: int) -> HTTPServer:
    """Start the local OAuth callback server in a background thread."""
    server = HTTPServer(("127.0.0.1", callback_port), _OAuthCallbackHandler)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# OAuth authorization flow
# ---------------------------------------------------------------------------

async def do_oauth_flow(
    client: httpx.AsyncClient,
    oauth_meta: dict,
    client_id: str,
    callback_port: int,
) -> dict:
    """Run the full OAuth authorization code flow with PKCE."""
    import hashlib
    import base64

    # Generate PKCE
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    state = secrets.token_urlsafe(32)

    # Start local callback server
    _OAuthCallbackHandler.code = None
    _OAuthCallbackHandler.state = None
    _run_callback_server(callback_port)

    redirect_uri = f"http://localhost:{callback_port}/oauth/callback"

    # Build authorization URL
    auth_params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "openid profile email",
        "resource": oauth_meta["resource"],
    })
    auth_url = f"{oauth_meta['authorization_endpoint']}?{auth_params}"

    # Open browser
    logger.info("Opening browser for authentication...")
    webbrowser.open(auth_url)

    # Wait for callback (up to 120 seconds)
    for _ in range(240):
        if _OAuthCallbackHandler.code:
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("OAuth timeout — no callback received within 120 seconds")

    if _OAuthCallbackHandler.state != state:
        raise RuntimeError("OAuth state mismatch")

    # Exchange code for tokens
    r = await client.post(
        oauth_meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": _OAuthCallbackHandler.code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    )
    r.raise_for_status()
    tokens = r.json()

    tokens["_client_id"] = client_id
    tokens["_token_endpoint"] = oauth_meta["token_endpoint"]
    tokens["_obtained_at"] = time.time()
    _save_cached_tokens(tokens)

    return tokens


async def refresh_tokens(client: httpx.AsyncClient, cached: dict) -> dict | None:
    """Refresh the access token, serialized across sibling bridge processes.

    Azure AD rotates refresh tokens on every refresh for public clients. If
    multiple bridges share ``TOKEN_CACHE_FILE``, a naive refresh in one
    bridge invalidates the refresh_token held by every other bridge.

    This function:
      1. Takes a cross-process exclusive lock on TOKEN_CACHE_LOCK_FILE.
      2. Re-reads the cache. If a sibling already rotated the token and
         the on-disk access_token is still valid, adopts it without
         calling Azure.
      3. Otherwise calls Azure with the freshest refresh_token (on-disk
         if newer than what the caller had in memory).
      4. Persists the result under the same lock.

    Returns the (possibly sibling-supplied) token dict, or ``None`` if
    refresh failed and no usable cached tokens are available.
    """
    if not cached.get("refresh_token") or not cached.get("_token_endpoint") or not cached.get("_client_id"):
        return None

    try:
        lock_fp = await _acquire_token_cache_lock(timeout=30.0)
    except TimeoutError as exc:
        logger.warning("Token refresh skipped: %s", exc)
        return None

    try:
        # Re-read cache under lock — a sibling may have just refreshed.
        on_disk = _load_cached_tokens()
        cached_obtained = cached.get("_obtained_at", 0)
        on_disk_obtained = on_disk.get("_obtained_at", 0) if on_disk else 0

        active = cached
        if on_disk and on_disk_obtained > cached_obtained:
            # Sibling refreshed since our last read.
            expires_in = on_disk.get("expires_in", 3600)
            if time.time() < on_disk_obtained + expires_in - 60:
                logger.info(
                    "Adopting sibling-refreshed tokens (obtained %.0fs ago, no Azure call)",
                    time.time() - on_disk_obtained,
                )
                return on_disk
            # Sibling's token is also expired — use its refresh_token (newest).
            logger.info("Sibling-refreshed tokens already expired; using their refresh_token")
            active = on_disk

        try:
            r = await client.post(
                active["_token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "client_id": active["_client_id"],
                    "refresh_token": active["refresh_token"],
                },
            )
        except Exception as exc:
            logger.warning("Token refresh HTTP error: %s", exc)
            return None

        if r.status_code != 200:
            logger.warning("Token refresh failed (HTTP %d)", r.status_code)
            # Last-chance check: a sibling may have rotated between our
            # re-read and our request (very narrow window even with lock).
            on_disk_again = _load_cached_tokens()
            if on_disk_again and on_disk_again.get("_obtained_at", 0) > cached_obtained:
                logger.info("Refresh failed but sibling-refreshed tokens are now on disk")
                return on_disk_again
            return None

        tokens = r.json()
        tokens["_client_id"] = active["_client_id"]
        tokens["_token_endpoint"] = active["_token_endpoint"]
        tokens["_obtained_at"] = time.time()
        _save_cached_tokens(tokens)
        return tokens
    finally:
        _funlock(lock_fp)
        lock_fp.close()


# ---------------------------------------------------------------------------
# Stdio <-> HTTP bridge
# ---------------------------------------------------------------------------

async def run_bridge(config: BridgeConfig | None = None):
    """Relay stdio to the configured HTTP endpoint; OAuth is explicitly opt-in."""
    config = config or load_config()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    logger.info("Epicor MCP OSS bridge starting (%s authentication)", config.auth_mode)
    verify = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True
    tokens = {}
    headers = {}
    if config.auth_mode == "token":
        headers["Authorization"] = f"Bearer {config.server_token}"
    elif config.auth_mode == "oauth":
        # A cache belongs to one server; switching hosts must never reuse tokens.
        global TOKEN_CACHE_FILE, TOKEN_CACHE_LOCK_FILE
        key = hashlib.sha256(config.server_url.encode()).hexdigest()[:20]
        TOKEN_CACHE_FILE = Path.home() / f".epicor_mcp_oss_{key}_tokens.json"
        TOKEN_CACHE_LOCK_FILE = Path.home() / f".epicor_mcp_oss_{key}_tokens.lock"
        async with httpx.AsyncClient(timeout=30, verify=verify) as client:
            tokens = _load_cached_tokens()
            if tokens and time.time() > tokens.get("_obtained_at", 0) + tokens.get("expires_in", 3600) - 60:
                tokens = await refresh_tokens(client, tokens) or {}
            if not tokens:
                meta = await discover_oauth(client, config.server_url)
                port = _find_free_oauth_port()
                client_id = await register_client(client, meta["registration_endpoint"], port)
                tokens = await do_oauth_flow(client, meta, client_id, port)
        headers["Authorization"] = f"Bearer {tokens['access_token']}"

    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(
        headers=headers, verify=verify, follow_redirects=False,
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=300),
    ) as http_client:
        async with streamable_http_client(
            url=config.server_url, http_client=http_client,
        ) as (read_stream, write_stream, _get_session_id):
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(_stdin_to_server(write_stream))
                    tg.create_task(_server_to_stdout(read_stream))
                    if config.auth_mode == "oauth":
                        tg.create_task(_token_refresh_loop(http_client, tokens))
            except* _BridgeShutdown:
                logger.info("Bridge stopped")


async def _token_refresh_loop(http_client, tokens: dict):
    """Proactively refresh the OAuth token before it expires.

    Runs as a background task alongside the stdin/stdout relay.
    Calculates sleep time from the token's actual expiry so a
    cached token obtained 50 minutes ago gets refreshed in ~5 min,
    not after a fixed 45-minute wait.

    On failure, retries after 2 minutes instead of waiting the full
    interval.  Never lets an unhandled exception propagate — that
    would kill the TaskGroup and take down the entire bridge.
    """
    REFRESH_BUFFER = 5 * 60   # Refresh 5 minutes before expiry
    RETRY_INTERVAL = 2 * 60   # On failure, retry after 2 minutes
    DEFAULT_EXPIRY = 3600     # Assume 1-hour tokens if not specified

    while True:
        try:
            # Calculate how long until the current token expires
            expires_in = tokens.get("expires_in", DEFAULT_EXPIRY)
            obtained_at = tokens.get("_obtained_at", time.time())
            expires_at = obtained_at + expires_in
            sleep_secs = max(30, expires_at - time.time() - REFRESH_BUFFER)

            logger.info(
                "Token refresh scheduled in %.0f minutes",
                sleep_secs / 60,
            )
            await asyncio.sleep(sleep_secs)

            logger.info("Proactive token refresh starting...")
            async with httpx.AsyncClient(timeout=30, verify=True) as refresh_client:
                refreshed = await refresh_tokens(refresh_client, tokens)

            if refreshed:
                new_token = refreshed.get("access_token", "")
                if new_token:
                    # httpx Headers are case-insensitive; use same case
                    # as the original to guarantee replacement, not duplication
                    http_client.headers["Authorization"] = f"Bearer {new_token}"
                    # Update tokens in place so next iteration uses new expiry/refresh_token
                    tokens.update(refreshed)
                    logger.info("Token refreshed successfully")
                else:
                    logger.warning("Token refresh returned empty access_token, retrying in %ds", RETRY_INTERVAL)
                    await asyncio.sleep(RETRY_INTERVAL)
            else:
                logger.warning("Token refresh failed, retrying in %ds", RETRY_INTERVAL)
                await asyncio.sleep(RETRY_INTERVAL)

        except asyncio.CancelledError:
            # TaskGroup is shutting down — exit cleanly, don't crash the group
            logger.info("Token refresh loop cancelled")
            return
        except Exception as exc:
            # Never let an exception propagate — it would kill the TaskGroup
            # and take down the stdin/stdout relay with it
            logger.error("Token refresh error: %s, retrying in %ds", exc, RETRY_INTERVAL)
            await asyncio.sleep(RETRY_INTERVAL)


async def _stdin_to_server(write_stream):
    """Read JSON-RPC messages from stdin and forward to the MCP server.

    Uses a thread to read stdin because Windows asyncio cannot read pipes
    via connect_read_pipe (ProactorEventLoop limitation).
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _read_stdin():
        """Blocking stdin reader running in a background thread."""
        try:
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    Thread(target=_read_stdin, daemon=True).start()

    while True:
        line = await queue.get()
        if line is None:
            # Stdin closed; shut down this client's bridge process.
            logger.info("stdin EOF — triggering bridge shutdown")
            raise _BridgeShutdown("stdin closed")
        line = line.strip()
        if not line:
            continue
        try:
            msg_dict = json.loads(line)
            # Log protocol metadata without logging argument values.
            method = msg_dict.get("method")
            msg_id = msg_dict.get("id")
            params = msg_dict.get("params") or {}
            tool_name = params.get("name") if method == "tools/call" else None
            arg_keys = (
                sorted((params.get("arguments") or {}).keys())
                if method == "tools/call" else None
            )
            logger.info(
                "STDIN -> id=%s method=%s tool=%s arg_keys=%s len=%d",
                msg_id, method, tool_name, arg_keys, len(line),
            )
            jsonrpc_msg = JSONRPCMessage.model_validate(msg_dict)
            session_msg = SessionMessage(message=jsonrpc_msg)
            await write_stream.send(session_msg)
        except Exception as exc:
            logger.error("Failed to forward stdin message: %s", exc)


async def _server_to_stdout(read_stream):
    """Read messages from the MCP server and write to stdout."""
    from mcp.shared.message import SessionMessage

    async for msg_or_exc in read_stream:
        if isinstance(msg_or_exc, Exception):
            logger.error("Server error: %s", msg_or_exc)
            continue
        if isinstance(msg_or_exc, SessionMessage):
            try:
                json_bytes = msg_or_exc.message.model_dump_json()
                sys.stdout.buffer.write(json_bytes.encode() if isinstance(json_bytes, str) else json_bytes)
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
            except Exception as exc:
                logger.error("Failed to write to stdout: %s", exc)

    # The server-side stream ended — there is no point keeping the bridge
    # alive (nothing left to relay). Trigger a clean shutdown.
    logger.info("server stream ended — triggering bridge shutdown")
    raise _BridgeShutdown("server disconnected")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Each client session owns a bridge process; stdin EOF ends that process.
    parser = argparse.ArgumentParser(description="Epicor MCP stdio-to-HTTP connector")
    parser.add_argument("--check", action="store_true", help="Validate configuration and bundled imports without connecting")
    parser.add_argument("--version", action="version", version="epicor-mcp-oss bridge 0.1.0")
    args = parser.parse_args()
    try:
        config = load_config()
        if args.check:
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.message import SessionMessage
            from mcp.types import JSONRPCMessage
            print("Configuration and connector imports OK", file=sys.stderr)
            return
        asyncio.run(run_bridge(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Bridge error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
