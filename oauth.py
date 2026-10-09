"""Minimal OAuth 2.1 authorization server for the Gotenberg MCP server.

Built on the MCP SDK's OAuthAuthorizationServerProvider: the SDK serves the
discovery metadata (RFC 8414 / RFC 9728), /register (RFC 7591 DCR), /authorize
(PKCE S256), /token and /revoke. This module stores clients and tokens and
renders a password-protected consent page at /oauth/login.

- Clients, access and refresh tokens are persisted (tokens only as SHA-256
  hashes) in a JSON file below DATA_DIR, so they survive restarts/redeploys.
- Refresh tokens are rotated on every use (required for public clients);
  replaying an already used refresh token after a short grace period
  (concurrent refreshes of a legitimate client) revokes the whole token family.
- Registration/pending logins are capped, wrong passwords are throttled per
  IP and globally (X-Forwarded-For can be spoofed, the global cap cannot).
- The static MCP_AUTH_TOKEN stays valid as bearer token next to OAuth tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("gotenberg-mcp.oauth")

LOGIN_PATH = "/oauth/login"
DEFAULT_SCOPES = ("mcp", "offline_access")
PENDING_TTL = 600  # seconds to complete the login page
CODE_TTL = 300  # authorization code lifetime
MAX_FAILURES = 10  # wrong passwords per IP ...
MAX_GLOBAL_FAILURES = 50  # ... or in total ...
FAILURE_WINDOW = 900  # ... within this many seconds -> locked
MAX_TRACKED_IPS = 10_000
MAX_CLIENTS = 1000  # oldest clients without live tokens are evicted beyond this
MAX_PENDING = 500
MAX_CODES = 500

# Login page texts; the language follows the browser's Accept-Language header (fallback: English)
TEXTS = {
    "en": {
        "title": "Gotenberg MCP – Sign in",
        "heading": "Allow access to Gotenberg MCP?",
        "wants": "<strong>{client}</strong> wants to use the PDF tools of this server.",
        "redirect": "After signing in you will be redirected to:",
        "warning": "Only allow access if you have just set up this connection yourself.",
        "password": "Password",
        "deny": "Deny",
        "allow": "Allow",
        "wrong_password": "Wrong password.",
        "expired_heading": "Sign-in expired",
        "expired_text": "Please start the connection in your client again.",
        "locked_heading": "Too many failed attempts",
        "locked_text": "Please try again later.",
    },
    "de": {
        "title": "Gotenberg MCP – Anmeldung",
        "heading": "Zugriff auf Gotenberg MCP erlauben?",
        "wants": "<strong>{client}</strong> möchte die PDF-Werkzeuge dieses Servers nutzen.",
        "redirect": "Nach der Anmeldung wirst du weitergeleitet zu:",
        "warning": "Erlaube den Zugriff nur, wenn du die Verbindung gerade selbst eingerichtet hast.",
        "password": "Passwort",
        "deny": "Ablehnen",
        "allow": "Erlauben",
        "wrong_password": "Falsches Passwort.",
        "expired_heading": "Anmeldung abgelaufen",
        "expired_text": "Bitte starte die Verbindung im Client erneut.",
        "locked_heading": "Zu viele Fehlversuche",
        "locked_text": "Bitte später erneut versuchen.",
    },
}
DEFAULT_LANG = "en"


def preferred_language(accept_language: str) -> str:
    """Pick the best supported language from an Accept-Language header (RFC 9110 q-values)."""
    candidates: list[tuple[float, int, str]] = []
    for index, part in enumerate(accept_language.split(",")):
        tag, _, params = part.strip().partition(";")
        quality = 1.0
        match = re.search(r"q\s*=\s*([^;\s]*)", params)
        if match:
            try:
                quality = float(match.group(1))  # invalid q-value -> entry ignored
            except ValueError:
                quality = 0.0
        primary = tag.strip().split("-")[0].lower()
        if quality > 0 and primary in TEXTS:
            candidates.append((-quality, index, primary))
    return min(candidates)[2] if candidates else DEFAULT_LANG


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class GotenbergOAuthProvider:
    def __init__(
        self,
        *,
        base_url: str,
        resource_url: str,
        password: str,
        static_token: str,
        state_file: Path,
        access_ttl: int,
        refresh_ttl: int,
        redirect_hosts: list[str],
        reuse_grace: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.resource_url = resource_url
        self.password = password.encode()
        self.static_token = static_token.encode()
        self.state_file = state_file
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.redirect_hosts = [h.lower() for h in redirect_hosts]
        self.reuse_grace = reuse_grace
        self._lock = threading.Lock()
        self.clients: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.used_refresh: dict[str, dict] = {}  # hash -> {family, expires_at}
        self.codes: dict[str, AuthorizationCode] = {}
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.failures: dict[str, list[float]] = {}
        self.global_failures: list[float] = []
        self._load()

    # ------------------------------------------------------------------ store

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("Cannot read OAuth state %s: %s (starting empty)", self.state_file, exc)
            return
        self.clients = data.get("clients", {})
        self.access = data.get("access", {})
        self.refresh = data.get("refresh", {})
        self.used_refresh = data.get("used_refresh", {})
        log.info("Loaded OAuth state: %d clients, %d refresh tokens", len(self.clients), len(self.refresh))

    def _snapshot(self) -> str:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if v["expires_at"] > now}
        self.refresh = {k: v for k, v in self.refresh.items() if v["expires_at"] > now}
        self.used_refresh = {k: v for k, v in self.used_refresh.items() if v["expires_at"] > now}
        return json.dumps(
            {"clients": self.clients, "access": self.access, "refresh": self.refresh, "used_refresh": self.used_refresh}
        )

    def _write(self, payload: str) -> None:
        with self._lock:  # serialize concurrent writers from the thread pool
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_file.with_suffix(".tmp")
                tmp.write_text(payload)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.state_file)
            except OSError as exc:
                log.error("Cannot write OAuth state %s: %s", self.state_file, exc)

    async def _save(self) -> None:
        # snapshot on the event loop (consistent), file I/O off the loop
        await asyncio.to_thread(self._write, self._snapshot())

    def _client_has_tokens(self, client_id: str) -> bool:
        return any(v["client_id"] == client_id for v in self.refresh.values()) or any(
            v["client_id"] == client_id for v in self.access.values()
        )

    def _evict_clients(self) -> None:
        if len(self.clients) < MAX_CLIENTS:
            return
        idle = sorted(
            (c.get("client_id_issued_at") or 0, cid) for cid, c in self.clients.items() if not self._client_has_tokens(cid)
        )
        for _, cid in idle[: len(self.clients) - MAX_CLIENTS + 1]:
            self.clients.pop(cid, None)

    async def _issue(self, client_id: str, scopes: list[str], resource: str | None, family: str = "") -> OAuthToken:
        now = int(time.time())
        family = family or secrets.token_hex(8)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        record = {"client_id": client_id, "scopes": scopes, "resource": resource or self.resource_url, "family": family}
        self.access[_hash(access_token)] = {**record, "expires_at": now + self.access_ttl}
        self.refresh[_hash(refresh_token)] = {**record, "expires_at": now + self.refresh_ttl}
        await self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self.access_ttl,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    def _drop_family(self, family: str) -> None:
        self.access = {k: v for k, v in self.access.items() if v.get("family") != family}
        self.refresh = {k: v for k, v in self.refresh.items() if v.get("family") != family}

    # --------------------------------------------------- provider protocol

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if self.redirect_hosts:
            for uri in client_info.redirect_uris or []:
                host = (urlsplit(str(uri)).hostname or "").lower()
                if host not in self.redirect_hosts:
                    raise RegistrationError("invalid_redirect_uri", f"redirect host '{host}' is not allowed")
        # be lenient with scopes: whatever the client registers plus our defaults
        requested = set((client_info.scope or "").split())
        client_info.scope = " ".join(sorted(requested | set(DEFAULT_SCOPES)))
        self._evict_clients()
        if len(self.clients) >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "too many registered clients")
        self.clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        await self._save()
        log.info("Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name or "unnamed")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        while len(self.pending) >= MAX_PENDING:  # drop oldest
            self.pending.pop(next(iter(self.pending)))
        request_id = secrets.token_urlsafe(32)
        self.pending[request_id] = (client.client_id, params, now + PENDING_TTL)
        return f"{self.base_url}{LOGIN_PATH}?req={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:  # single use, also under concurrency
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def _reuse_detected(self, token_hash: str) -> None:
        used = self.used_refresh.get(token_hash)
        # within the grace period this is most likely a parallel refresh of the same client
        if used and time.time() - used.get("used_at", 0) > self.reuse_grace:
            log.warning("Refresh token replay detected: revoking token family %s", used["family"])
            self._drop_family(used["family"])
            await self._save()

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        token_hash = _hash(refresh_token)
        if token_hash in self.used_refresh:
            await self._reuse_detected(token_hash)
            return None
        rec = self.refresh.get(token_hash)
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            expires_at=rec["expires_at"],
            resource=rec.get("resource"),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]):
        token_hash = _hash(refresh_token.token)
        rec = self.refresh.pop(token_hash, None)  # atomic on the event loop: only one exchange wins
        if rec is None:
            await self._reuse_detected(token_hash)
            raise TokenError("invalid_grant", "refresh token already used")
        family = rec["family"]
        # rotation: old access tokens die, the old refresh token is remembered for replay detection
        self.access = {k: v for k, v in self.access.items() if v.get("family") != family}
        self.used_refresh[token_hash] = {"family": family, "expires_at": rec["expires_at"], "used_at": time.time()}
        return await self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.static_token and hmac.compare_digest(token.encode(), self.static_token):
            return AccessToken(token=token, client_id="static-token", scopes=list(DEFAULT_SCOPES), resource=self.resource_url)
        rec = self.access.get(_hash(token))
        if not rec or rec["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=rec["client_id"],
            scopes=rec["scopes"],
            expires_at=rec["expires_at"],
            resource=rec.get("resource"),
        )

    async def revoke_token(self, token) -> None:
        rec = self.access.get(_hash(token.token)) or self.refresh.get(_hash(token.token))
        if rec:
            self._drop_family(rec["family"])
            await self._save()

    # ------------------------------------------------------- consent page

    def _client_ip(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _locked(self, ip: str) -> bool:
        now = time.time()
        self.global_failures = [t for t in self.global_failures if t > now - FAILURE_WINDOW]
        if len(self.global_failures) >= MAX_GLOBAL_FAILURES:
            return True
        recent = [t for t in self.failures.get(ip, []) if t > now - FAILURE_WINDOW]
        if recent:
            self.failures[ip] = recent
        else:
            self.failures.pop(ip, None)
        return len(recent) >= MAX_FAILURES

    def _record_failure(self, ip: str) -> None:
        now = time.time()
        self.global_failures.append(now)
        if ip not in self.failures and len(self.failures) >= MAX_TRACKED_IPS:
            self.failures.pop(next(iter(self.failures)))  # drop oldest tracked IP
        self.failures.setdefault(ip, []).append(now)

    @staticmethod
    def _form_action_source(redirect_uri: str) -> str:
        """CSP source for the redirect target (browsers apply form-action to the post-submit redirect)."""
        parts = urlsplit(redirect_uri)
        scheme = parts.scheme.lower()
        if not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
            return ""
        if scheme in ("http", "https"):
            netloc = parts.netloc.rsplit("@", 1)[-1]
            if re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", netloc):
                return f"{scheme}://{netloc}"
            return ""
        return f"{scheme}:"

    def _page(self, lang: str, body: str, status: int = 200, redirect_uri: str = "") -> HTMLResponse:
        form_action = " ".join(filter(None, ["'self'", self._form_action_source(redirect_uri)]))
        doc = f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TEXTS[lang]["title"]}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f5f5f4; --card:#fff; --fg:#1c1917; --muted:#57534e; --accent:#2563eb; --border:#d6d3d1; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1917; --card:#292524; --fg:#f5f5f4; --muted:#a8a29e; --accent:#60a5fa; --border:#44403c; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; box-sizing:border-box; }}
main {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:28px; max-width:420px; width:100%; box-sizing:border-box; }}
h1 {{ font-size:1.25rem; margin:0 0 12px; }}
p {{ color:var(--muted); margin:8px 0; }}
.host {{ font-family:ui-monospace,Menlo,monospace; color:var(--fg); font-weight:600; word-break:break-all; }}
label {{ display:block; margin:18px 0 6px; font-weight:600; }}
input[type=password] {{ width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--fg); font-size:1rem; }}
.row {{ display:flex; gap:10px; margin-top:18px; }}
button {{ flex:1; padding:10px; border-radius:8px; border:1px solid var(--border); font-size:1rem; cursor:pointer; background:transparent; color:var(--fg); }}
button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.err {{ color:#dc2626; font-weight:600; }}
</style></head><body><main>{body}</main></body></html>"""
        return HTMLResponse(
            doc,
            status_code=status,
            headers={
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; frame-ancestors 'none'",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Language": lang,
                "Vary": "Accept-Language",
            },
        )

    def _message(self, lang: str, key: str, status: int) -> HTMLResponse:
        t = TEXTS[lang]
        return self._page(lang, f"<h1>{t[key + '_heading']}</h1><p>{t[key + '_text']}</p>", status)

    def _form(self, lang: str, request_id: str, client_name: str, redirect_uri: str, error: bool = False) -> HTMLResponse:
        t = TEXTS[lang]
        redirect_host = urlsplit(redirect_uri).netloc or redirect_uri
        err = f'<p class="err">{t["wrong_password"]}</p>' if error else ""
        body = f"""<h1>{t["heading"]}</h1>
<p>{t["wants"].format(client=html.escape(client_name))}</p>
<p>{t["redirect"]}<br><span class="host">{html.escape(redirect_host)}</span></p>
<p>{t["warning"]}</p>
{err}
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="req" value="{html.escape(request_id)}">
<label for="pw">{t["password"]}</label>
<input id="pw" type="password" name="password" autocomplete="current-password" autofocus>
<div class="row"><button type="submit" name="action" value="deny">{t["deny"]}</button>
<button class="primary" type="submit" name="action" value="allow">{t["allow"]}</button></div>
</form>"""
        return self._page(lang, body, 401 if error else 200, redirect_uri)

    def _pending(self, request_id: str):
        entry = self.pending.get(request_id)
        if not entry or entry[2] < time.time():
            return None
        return entry

    async def login(self, request: Request) -> Response:
        lang = preferred_language(request.headers.get("accept-language", ""))
        if request.method == "GET":
            request_id = request.query_params.get("req", "")
        else:
            form = await request.form()
            request_id = str(form.get("req", ""))
        entry = self._pending(request_id)
        if not entry:
            return self._message(lang, "expired", 400)
        client_id, params, _ = entry
        client = self.clients.get(client_id, {})
        client_name = client.get("client_name") or client_id
        redirect_uri = str(params.redirect_uri)

        if request.method == "GET":
            return self._form(lang, request_id, client_name, redirect_uri)

        if form.get("action") == "deny":
            self.pending.pop(request_id, None)
            return RedirectResponse(
                construct_redirect_uri(redirect_uri, error="access_denied", state=params.state), status_code=302
            )

        ip = self._client_ip(request)
        if self._locked(ip):
            return self._message(lang, "locked", 429)
        password = str(form.get("password", "")).encode()
        if not self.password or not hmac.compare_digest(password, self.password):
            self._record_failure(ip)
            log.warning("OAuth login: wrong password from %s", ip)
            await asyncio.sleep(1)
            return self._form(lang, request_id, client_name, redirect_uri, error=True)

        self.pending.pop(request_id, None)
        self.failures.pop(ip, None)
        now = time.time()
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        while len(self.codes) >= MAX_CODES:
            self.codes.pop(next(iter(self.codes)))
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        log.info("OAuth login granted for client %s (%s)", client_id, client_name)
        return RedirectResponse(construct_redirect_uri(redirect_uri, code=code, state=params.state), status_code=302)
