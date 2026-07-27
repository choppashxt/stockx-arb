"""One-time interactive OAuth authorization-code flow.

The human logs in to StockX in their own browser; this module only builds the
authorize URL, catches the redirect (or accepts it pasted), exchanges the code
for tokens, and stores the refresh token in .env. No credentials pass through
here — just the authorization code.
"""
from __future__ import annotations

import asyncio
import errno
import os
import secrets as pysecrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from ..config import PROJECT_ROOT, Secrets
from .auth import AUDIENCE, TOKEN_URL

AUTHORIZE_URL = "https://accounts.stockx.com/authorize"
DEFAULT_PORT = 8123
WIN_EADDRINUSE = 10048


def say(msg: str = "") -> None:
    """Print and flush. stdout is block-buffered whenever it isn't a terminal
    (piped output, IDE run buttons), which would otherwise hide every prompt
    below until the command exits — ten minutes later."""
    print(msg, flush=True)


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "offline_access openid",
        "audience": AUDIENCE,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def code_from_pasted_url(pasted: str, state: str) -> str:
    params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
    code = params.get("code", [""])[0]
    got_state = params.get("state", [""])[0]
    if not code:
        raise SystemExit("no ?code= found in that URL — paste the FULL address "
                         "the browser landed on after login")
    if got_state != state:
        raise SystemExit("state mismatch — start over (possible stale/foreign URL)")
    return code


async def start_callback_server(port: int, state: str
                                ) -> tuple[asyncio.AbstractServer,
                                           "asyncio.Future[str]"]:
    """Bind the one-shot localhost listener BEFORE sending the user to log in,
    so a port clash surfaces now rather than after they've authorized."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            while await reader.readline() not in (b"\r\n", b""):
                pass
            path = request_line.split()[1].decode(errors="replace")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            code = params.get("code", [""])[0]
            got_state = params.get("state", [""])[0]
            oauth_error = params.get("error", [""])[0]

            if oauth_error:
                # StockX/Auth0 redirected back with a refusal — say so loudly
                # instead of sitting here until the timeout.
                detail = params.get("error_description", [""])[0]
                problem = f"{oauth_error}: {detail}" if detail else oauth_error
            elif not code:
                problem = f"no ?code= in the callback (path was {path[:120]})"
            elif got_state != state:
                problem = ("state mismatch — the login used a URL from an "
                           "older run; use the newest URL printed below")
            else:
                problem = ""

            say(f"callback received{'' if not problem else ' — PROBLEM'}")
            body = ("<h2>stockx-arb: authorized — you can close this tab.</h2>"
                    if not problem else
                    f"<h2>Authorization problem</h2><p>{problem}</p>"
                    "<p>Check the terminal.</p>")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         b"Connection: close\r\n\r\n" + body.encode())
            await writer.drain()
            writer.close()
            if result.done():
                return
            if problem:
                say(f"  {problem}")
                result.set_exception(SystemExit(f"authorization failed — {problem}"))
            else:
                result.set_result(code)
        except Exception as e:
            say(f"callback handler error: {e}")

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", port)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, WIN_EADDRINUSE):
            raise SystemExit(
                f"port {port} is already in use — almost certainly an earlier "
                f"'arb auth' run still waiting for a login.\n"
                f"Close that window, or run:  python -m arb auth --manual")
        raise
    return server, result


PASTE_FILE_TEMPLATE = """\
=== stockx-arb: paste the callback URL below, then SAVE and CLOSE this file ===

1. Finish logging in to StockX in your browser.
2. The browser will land on a page that fails to load — that is EXPECTED.
3. Copy the ENTIRE address from the browser's address bar.
4. Paste it on the empty line below, then press Ctrl+S and close Notepad.

(This file is deleted automatically once the URL is read — it contains a
 one-time login code, so don't copy it anywhere else.)

PASTE HERE >>>
"""


async def wait_for_pasted_file(path: Path, state: str,
                               timeout_s: int = 600) -> str:
    """Terminal-free fallback: the user pastes the callback URL into a text
    file. Avoids needing an interactive stdin, and keeps the one-time code off
    the command line and out of any chat transcript."""
    path.write_text(PASTE_FILE_TEMPLATE, encoding="utf-8")
    say(f"Opening a file for you to paste into:\n  {path}")
    try:
        os.startfile(str(path))          # noqa: S606 - Windows: opens Notepad
    except Exception:
        say("(couldn't open it automatically — open that file yourself)")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("http") and "code=" in line:
                return code_from_pasted_url(line, state)
    raise SystemExit("timed out waiting for the pasted URL")


async def exchange_code(secrets: Secrets, code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": secrets.stockx_client_id,
            "client_secret": secrets.stockx_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        })
    if resp.status_code != 200:
        raise SystemExit(f"token exchange failed: HTTP {resp.status_code} — "
                         f"{resp.text[:300]}")
    return resp.json()


def save_refresh_token(token: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    key = "STOCKX_REFRESH_TOKEN="
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(key):
            lines[i] = key + token
            replaced = True
            break
    if not replaced:
        lines.append(key + token)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_auth_flow(manual: bool, port: int = DEFAULT_PORT,
                        redirect_uri: str | None = None) -> None:
    secrets = Secrets()
    missing = [n for n, v in [("STOCKX_CLIENT_ID", secrets.stockx_client_id),
                              ("STOCKX_CLIENT_SECRET", secrets.stockx_client_secret)]
               if not v or v.startswith("PASTE_")]
    if missing:
        raise SystemExit(
            f"fill in {', '.join(missing)} in .env first (Applications page on "
            "developer.stockx.com — create an app if you have none)")

    # must match the app's registered Callback URI EXACTLY; override with
    # --redirect-uri when the app is registered with something else
    redirect_uri = redirect_uri or f"http://localhost:{port}/callback"
    state = pysecrets.token_urlsafe(16)
    url = build_authorize_url(secrets.stockx_client_id, redirect_uri, state)

    server = result = None
    if not manual:
        server, result = await start_callback_server(port, state)

    say()
    say("=" * 70)
    say("STEP 1 — your app's Callback URI on developer.stockx.com must be:")
    say(f"           {redirect_uri}")
    say("STEP 2 — open this URL and log in to StockX:")
    say()
    say(url)
    say("=" * 70)
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    say("(opened in your default browser)" if opened else
        "(could not open a browser automatically — copy the URL above)")

    paste_file = PROJECT_ROOT / "paste-callback-url.txt"
    try:
        if manual:
            say()
            say("After login the browser lands on the callback URL "
                "(an error page is fine).")
            if sys.stdin is not None and sys.stdin.isatty():
                pasted = input("Paste that FULL URL here: ")
                code = code_from_pasted_url(pasted, state)
            else:
                # no interactive stdin (piped runner / IDE) -> paste into a file
                code = await wait_for_pasted_file(paste_file, state)
        else:
            say()
            say(f"Waiting up to 10 min for the login redirect on port {port} …")
            say("(Ctrl+C to abort; or re-run with --manual to paste the URL)")
            code = await asyncio.wait_for(result, 600)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        # the paste file holds a one-time login code — never leave it lying about
        paste_file.unlink(missing_ok=True)

    tokens = await exchange_code(secrets, code, redirect_uri)
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise SystemExit("no refresh_token in the response — the app must grant "
                         "scope 'offline_access openid' (got: "
                         f"{list(tokens.keys())})")
    save_refresh_token(refresh)
    say()
    say("refresh token saved to .env — StockX auth is DONE.")
    say("Smoke test:  python -m arb resolve DZ5485-612 --market")


# Unbuffered stdout for this module's callers regardless of how they were
# launched; harmless if stdout has already been reconfigured.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
