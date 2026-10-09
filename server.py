"""MCP server for Gotenberg (document conversion API).

Transports:
  MCP_TRANSPORT=streamable-http (default) -> http://0.0.0.0:8000/mcp
  MCP_TRANSPORT=stdio                     -> JSON-RPC on stdin/stdout

stdout is reserved for JSON-RPC in stdio mode: all logging goes to stderr,
there are no print() calls in this module.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import Field

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


# MCP_LOG_LEVEL, not LOG_LEVEL: Gotenberg also reads LOG_LEVEL (lowercase only) and refuses
# to start if a platform like Coolify passes our value on to it. LOG_LEVEL stays as fallback.
LOG_LEVEL = (_env("MCP_LOG_LEVEL") or _env("LOG_LEVEL", "INFO")).upper()
logging.basicConfig(
    level=LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("gotenberg-mcp")

TRANSPORT = _env("MCP_TRANSPORT", "streamable-http")
HOST = _env("MCP_HOST", "0.0.0.0")
PORT = int(_env("MCP_PORT", "8000"))
MCP_AUTH_TOKEN = _env("MCP_AUTH_TOKEN")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
ALLOWED_HOSTS = _env("ALLOWED_HOSTS")

GOTENBERG_URL = _env("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
GOTENBERG_USERNAME = _env("GOTENBERG_USERNAME")
GOTENBERG_PASSWORD = _env("GOTENBERG_PASSWORD")
GOTENBERG_TIMEOUT = float(_env("GOTENBERG_TIMEOUT", "120"))

DATA_DIR = Path(_env("DATA_DIR", "/data"))
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
HOST_DATA_DIR = _env("HOST_DATA_DIR").rstrip("/")

FILE_TTL_HOURS = float(_env("FILE_TTL_HOURS", "24") or "24")
MAX_INPUT_MB = float(_env("MAX_INPUT_MB", "50") or "50")
MAX_INPUT_BYTES = int(MAX_INPUT_MB * 1024 * 1024)
ALLOW_PRIVATE_URLS = _env_bool("ALLOW_PRIVATE_URLS", False)

# OAuth 2.1 (for Claude / Mistral "custom connectors"); needs a public base URL
OAUTH_ENABLED = _env_bool("OAUTH_ENABLED", True)
OAUTH_PASSWORD = _env("OAUTH_PASSWORD") or MCP_AUTH_TOKEN
OAUTH_ACCESS_TOKEN_TTL_MINUTES = int(_env("OAUTH_ACCESS_TOKEN_TTL_MINUTES", "60") or "60")
OAUTH_REFRESH_TOKEN_TTL_DAYS = int(_env("OAUTH_REFRESH_TOKEN_TTL_DAYS", "30") or "30")
OAUTH_REDIRECT_HOSTS = [h.strip() for h in _env("OAUTH_REDIRECT_HOSTS").split(",") if h.strip()]
OAUTH_REFRESH_REUSE_GRACE_SECONDS = int(_env("OAUTH_REFRESH_REUSE_GRACE_SECONDS", "10") or "10")

_signing_key = _env("FILE_SIGNING_KEY") or MCP_AUTH_TOKEN
if not _signing_key:
    _signing_key = secrets.token_urlsafe(48)
    log.warning(
        "Neither FILE_SIGNING_KEY nor MCP_AUTH_TOKEN set: using a random signing key, "
        "download links become invalid after a restart."
    )
SIGNING_KEY = _signing_key.encode()

PAPER_SIZES_IN: dict[str, tuple[float, float]] = {
    "A3": (11.7, 16.54),
    "A4": (8.27, 11.7),
    "A5": (5.83, 8.27),
    "Letter": (8.5, 11.0),
    "Legal": (8.5, 14.0),
}

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
WAIT_DELAY_RE = re.compile(r"^\d+(\.\d+)?(ms|s|m)$")
EXP_RE = re.compile(r"[0-9]{1,12}")
MAX_MERGE_INPUTS = 50

# --------------------------------------------------------------------------- #
# FastMCP setup (feature-detect kwargs so any mcp>=1.9,<2 works)
# --------------------------------------------------------------------------- #

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402


def _transport_security():
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # SDK too old -> no host validation available
        return None

    if ALLOWED_HOSTS == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts = [h.strip() for h in ALLOWED_HOSTS.split(",") if h.strip()]
    if not hosts and PUBLIC_BASE_URL:
        netloc = urlsplit(PUBLIC_BASE_URL).netloc
        if netloc:
            hostname = netloc.split(":")[0]
            hosts = [netloc, hostname, f"{hostname}:*"]
    if not hosts:
        log.warning("ALLOWED_HOSTS and PUBLIC_BASE_URL empty: Host header validation disabled.")
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    origins: list[str] = []
    for h in hosts:
        base = h[:-2] if h.endswith(":*") else h
        for scheme in ("https", "http"):
            origins.append(f"{scheme}://{h}")
            origins.append(f"{scheme}://{base}")
    log.info("Allowed hosts: %s", ", ".join(hosts))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=sorted(set(origins)),
    )


_fastmcp_kwargs = {
    "host": HOST,
    "port": PORT,
    "stateless_http": True,
    "log_level": LOG_LEVEL if LOG_LEVEL in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO",
    "transport_security": _transport_security(),
    # base64 payloads are ~4/3 of the binary size; leave room for several files
    "max_request_body_size": int(MAX_INPUT_BYTES * 2 + 1024 * 1024),
}


def _oauth_setup():
    """Return (provider, AuthSettings) or (None, None) if OAuth is not active."""
    if TRANSPORT == "stdio" or not OAUTH_ENABLED:
        return None, None
    if not PUBLIC_BASE_URL or not OAUTH_PASSWORD:
        log.warning("OAuth disabled: needs PUBLIC_BASE_URL and OAUTH_PASSWORD (or MCP_AUTH_TOKEN)")
        return None, None
    parts = urlsplit(PUBLIC_BASE_URL)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        log.warning("OAuth disabled: PUBLIC_BASE_URL must use https (got %s)", PUBLIC_BASE_URL)
        return None, None
    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

        from oauth import GotenbergOAuthProvider
    except ImportError as exc:
        log.warning("OAuth disabled: SDK auth support missing (%s)", exc)
        return None, None

    resource_url = f"{PUBLIC_BASE_URL}/mcp"
    provider = GotenbergOAuthProvider(
        base_url=PUBLIC_BASE_URL,
        resource_url=resource_url,
        password=OAUTH_PASSWORD,
        static_token=MCP_AUTH_TOKEN,
        state_file=DATA_DIR / "oauth" / "state.json",
        access_ttl=OAUTH_ACCESS_TOKEN_TTL_MINUTES * 60,
        refresh_ttl=OAUTH_REFRESH_TOKEN_TTL_DAYS * 86400,
        redirect_hosts=OAUTH_REDIRECT_HOSTS,
        reuse_grace=OAUTH_REFRESH_REUSE_GRACE_SECONDS,
    )
    settings = {
        "issuer_url": PUBLIC_BASE_URL,
        "resource_server_url": resource_url,
        "client_registration_options": ClientRegistrationOptions(enabled=True),
        "revocation_options": RevocationOptions(enabled=True),
    }
    if "validate_token_resource" in AuthSettings.model_fields:
        # tokens come only from our own store and are only valid here
        settings["validate_token_resource"] = False
    log.info("OAuth enabled: issuer %s, resource %s", PUBLIC_BASE_URL, resource_url)
    return provider, AuthSettings(**settings)


oauth_provider, _auth_settings = _oauth_setup()
if oauth_provider:
    _fastmcp_kwargs["auth_server_provider"] = oauth_provider
    _fastmcp_kwargs["auth"] = _auth_settings

_supported = inspect.signature(FastMCP.__init__).parameters
_missing = sorted(k for k in _fastmcp_kwargs if k not in _supported)
if _missing:
    # never start with silently dropped settings (e.g. auth -> /mcp would be open)
    log.critical("Installed MCP SDK does not support FastMCP(%s); install mcp>=1.30,<2", ", ".join(_missing))
    sys.exit(1)
mcp = FastMCP(
    "gotenberg",
    instructions=(
        "Convert URLs, HTML, Markdown and office documents to PDF and merge PDFs "
        "using a Gotenberg instance. Results are returned as JSON with a download_url."
    ),
    **_fastmcp_kwargs,
)

_tool_kwargs = {}
if "structured_output" in inspect.signature(mcp.tool).parameters:
    # plain JSON text result, no outputSchema -> maximum client compatibility
    _tool_kwargs["structured_output"] = False


def tool(func):
    return mcp.tool(**_tool_kwargs)(func)


# --------------------------------------------------------------------------- #
# Helpers: files, signatures, inputs
# --------------------------------------------------------------------------- #


def _ensure_dirs() -> None:
    for d in (INPUT_DIR, OUTPUT_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Cannot create %s: %s", d, exc)


def _sign(file_id: str, exp: int) -> str:
    return hmac.new(SIGNING_KEY, f"{file_id}.{exp}".encode(), hashlib.sha256).hexdigest()


def _verify(file_id: str, exp: str, sig: str) -> bool:
    if not FILE_ID_RE.match(file_id) or not EXP_RE.fullmatch(exp) or not sig:
        return False
    if int(exp) < time.time():
        return False
    return hmac.compare_digest(_sign(file_id, int(exp)), sig)


def _safe_filename(name: str, default: str, ext: str = "") -> str:
    name = Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or default
    name = name[:120]
    if ext and not name.lower().endswith(ext):
        name += ext
    return name


def _output_paths(file_id: str) -> tuple[Path, Path]:
    return OUTPUT_DIR / f"{file_id}.pdf", OUTPUT_DIR / f"{file_id}.json"


def _store_result(content: bytes, filename: str, trace: str, return_base64: bool) -> str:
    _ensure_dirs()
    file_id = secrets.token_urlsafe(18)
    exp = int(time.time() + FILE_TTL_HOURS * 3600)
    pdf_path, meta_path = _output_paths(file_id)
    pdf_path.write_bytes(content)
    meta_path.write_text(json.dumps({"filename": filename, "expires": exp}))

    download_url = ""
    if PUBLIC_BASE_URL:
        download_url = f"{PUBLIC_BASE_URL}/files/{file_id}?exp={exp}&sig={_sign(file_id, exp)}"

    result: dict = {
        "download_url": download_url,
        "filename": filename,
        "bytes": len(content),
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        "trace": trace,
        "file_id": file_id,
    }
    if HOST_DATA_DIR:
        result["local_path"] = f"{HOST_DATA_DIR}/output/{file_id}.pdf"
    if return_base64:
        result["base64"] = base64.b64encode(content).decode()
    log.info("Stored %s (%d bytes, trace %s)", filename, len(content), trace)
    return json.dumps(result, ensure_ascii=False)


def _cleanup_once() -> None:
    now = time.time()
    if not OUTPUT_DIR.is_dir():
        return
    for meta in OUTPUT_DIR.glob("*.json"):
        try:
            exp = json.loads(meta.read_text()).get("expires", 0)
        except (OSError, ValueError):
            exp = 0
        if exp < now:
            meta.with_suffix(".pdf").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            log.info("Deleted expired file %s", meta.stem)
    # orphaned PDFs without metadata
    for pdf in OUTPUT_DIR.glob("*.pdf"):
        if not pdf.with_suffix(".json").exists() and pdf.stat().st_mtime + FILE_TTL_HOURS * 3600 < now:
            pdf.unlink(missing_ok=True)


def _cleanup_loop() -> None:
    while True:
        try:
            _cleanup_once()
        except Exception as exc:  # never kill the thread
            log.warning("Cleanup failed: %s", exc)
        time.sleep(600)


def _resolve_input_path(rel: str) -> Path:
    base = INPUT_DIR.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ToolError(f"input_path '{rel}' is outside of {INPUT_DIR}")
    if not target.is_file():
        raise ToolError(f"input_path '{rel}' not found in {INPUT_DIR}")
    if target.stat().st_size > MAX_INPUT_BYTES:
        raise ToolError(f"input_path '{rel}' exceeds MAX_INPUT_MB={MAX_INPUT_MB:g}")
    return target


def _own_file_from_ref(ref: str) -> tuple[bytes, str] | None:
    """Resolve a file_id or a download link of this server to local content."""
    if FILE_ID_RE.match(ref):
        file_id, verified = ref, True
    else:
        parts = urlsplit(ref)
        own_netloc = urlsplit(PUBLIC_BASE_URL).netloc.lower() if PUBLIC_BASE_URL else ""
        if not own_netloc or parts.netloc.lower() != own_netloc:
            return None  # foreign URL -> regular download
        m = re.fullmatch(r"/files/([A-Za-z0-9_-]+)", parts.path)
        if not m:
            return None
        qs = parse_qs(parts.query)
        if "sig" not in qs or "exp" not in qs:
            return None
        file_id = m.group(1)
        verified = _verify(file_id, qs["exp"][0], qs["sig"][0])
        if not verified:
            raise ToolError(f"Download link for {file_id} is invalid or expired")
    pdf_path, meta_path = _output_paths(file_id)
    if not pdf_path.is_file():
        if FILE_ID_RE.match(ref):
            return None  # not ours, treat as something else
        raise ToolError(f"File {file_id} no longer exists")
    try:
        name = json.loads(meta_path.read_text()).get("filename", f"{file_id}.pdf")
    except (OSError, ValueError):
        name = f"{file_id}.pdf"
    return pdf_path.read_bytes(), name


def _check_ip(ip_str: str, host: str) -> None:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise ToolError(f"URL host '{host}' resolves to non-public address {ip} (blocked)")


async def _assert_public_url(url: str) -> str:
    """Validate scheme/host and return a checked public IP to connect to ("" if private URLs are allowed)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs are allowed")
    host = parts.hostname
    if not host:
        raise ToolError("URL has no host")
    if parts.username or parts.password:
        raise ToolError("URLs with credentials are not allowed")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise ToolError("URL has an invalid port") from None
    if ALLOW_PRIVATE_URLS:
        return ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"Cannot resolve host '{host}': {exc}") from None
    if not infos:
        raise ToolError(f"Cannot resolve host '{host}'")
    for info in infos:
        _check_ip(info[4][0], host)
    return infos[0][4][0].split("%")[0]


def _pinned_request(url: str, ip: str) -> tuple[str, dict[str, str], dict]:
    """Rewrite url to connect to ip while keeping Host header and TLS SNI/cert check on the hostname."""
    if not ip:
        return url, {}, {}
    parts = urlsplit(url)
    ip_host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{ip_host}:{parts.port}" if parts.port else ip_host
    target = urlunsplit(parts._replace(netloc=netloc))
    # netloc has no userinfo (rejected above), so it is exactly the original Host value
    return target, {"Host": parts.netloc}, {"sni_hostname": parts.hostname}


async def _download(url: str) -> tuple[bytes, str]:
    """Download with SSRF check on every redirect hop and a size limit."""
    current = url
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for _ in range(6):
            ip = await _assert_public_url(current)
            target, headers, extensions = _pinned_request(current, ip)
            async with client.stream("GET", target, headers=headers, extensions=extensions) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise ToolError("Redirect without Location header")
                    current = urljoin(current, location)
                    continue
                if resp.status_code >= 400:
                    raise ToolError(f"Download of {current} failed: HTTP {resp.status_code}")
                length = resp.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_INPUT_BYTES:
                    raise ToolError(f"Input exceeds MAX_INPUT_MB={MAX_INPUT_MB:g}")
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_INPUT_BYTES:
                        raise ToolError(f"Input exceeds MAX_INPUT_MB={MAX_INPUT_MB:g}")
                    chunks.append(chunk)
                name = ""
                cd = resp.headers.get("content-disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                if m:
                    name = m.group(1)
                if not name:
                    name = Path(urlsplit(current).path).name
                return b"".join(chunks), name
        raise ToolError("Too many redirects")


def _decode_b64(data: str) -> bytes:
    if "," in data[:100] and data.startswith("data:"):
        data = data.split(",", 1)[1]
    if len(data) * 3 / 4 > MAX_INPUT_BYTES + 4:
        raise ToolError(f"Input exceeds MAX_INPUT_MB={MAX_INPUT_MB:g}")
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        raise ToolError("file_base64 is not valid base64") from None


async def _load_single_input(input_url: str, file_base64: str, source_filename: str, input_path: str) -> tuple[bytes, str]:
    given = [bool(input_url), bool(file_base64), bool(input_path)]
    if sum(given) != 1:
        raise ToolError("Provide exactly one of input_url, file_base64 (+ source_filename) or input_path")
    if input_url:
        own = _own_file_from_ref(input_url)
        content, name = own if own else await _download(input_url)
        name = source_filename or name
    elif file_base64:
        if not source_filename:
            raise ToolError("source_filename (with extension, e.g. 'report.docx') is required with file_base64")
        content, name = _decode_b64(file_base64), source_filename
    else:
        path = _resolve_input_path(input_path)
        content, name = path.read_bytes(), source_filename or path.name
    if not content:
        raise ToolError("Input is empty")
    return content, name


# --------------------------------------------------------------------------- #
# Gotenberg client
# --------------------------------------------------------------------------- #


def _gotenberg_client() -> httpx.AsyncClient:
    auth = (GOTENBERG_USERNAME, GOTENBERG_PASSWORD) if GOTENBERG_USERNAME else None
    return httpx.AsyncClient(base_url=GOTENBERG_URL, auth=auth, timeout=GOTENBERG_TIMEOUT)


@dataclass
class GotenbergResult:
    content: bytes
    trace: str


async def _gotenberg_post(route: str, data: dict[str, str], files: list[tuple]) -> GotenbergResult:
    trace = secrets.token_hex(8)
    try:
        async with _gotenberg_client() as client:
            # always multipart (Gotenberg rejects urlencoded bodies, e.g. for convert/url)
            parts = [(k, (None, v)) for k, v in data.items()] + files
            resp = await client.post(route, files=parts, headers={"Gotenberg-Trace": trace})
    except httpx.TimeoutException:
        raise ToolError(f"Gotenberg timed out after {GOTENBERG_TIMEOUT:g}s ({route})") from None
    except httpx.HTTPError as exc:
        raise ToolError(f"Gotenberg not reachable at {GOTENBERG_URL}: {exc.__class__.__name__}: {exc}") from None
    trace = resp.headers.get("Gotenberg-Trace", trace)
    if resp.status_code != 200:
        body = resp.text.strip().replace("\n", " ")[:500]
        raise ToolError(f"Gotenberg error {resp.status_code} on {route} (trace {trace}): {body}")
    if not resp.content.startswith(b"%PDF"):
        raise ToolError(f"Gotenberg returned no PDF on {route} (trace {trace})")
    return GotenbergResult(resp.content, trace)


def _chromium_form(paper: str, landscape: bool, margin_cm: float, print_background: bool, wait_delay: str) -> dict[str, str]:
    if paper not in PAPER_SIZES_IN:
        raise ToolError(f"paper must be one of {', '.join(PAPER_SIZES_IN)}")
    if margin_cm < 0 or margin_cm > 10:
        raise ToolError("margin_cm must be between 0 and 10")
    width, height = PAPER_SIZES_IN[paper]
    margin = f"{margin_cm / 2.54:.3f}"
    form = {
        "paperWidth": str(width),
        "paperHeight": str(height),
        "marginTop": margin,
        "marginBottom": margin,
        "marginLeft": margin,
        "marginRight": margin,
        "landscape": str(landscape).lower(),
        "printBackground": str(print_background).lower(),
    }
    if wait_delay:
        if not WAIT_DELAY_RE.match(wait_delay):
            raise ToolError("wait_delay must be empty or a duration like '500ms' or '2s'")
        form["waitDelay"] = wait_delay
    return form


# --------------------------------------------------------------------------- #
# Tool parameter types (flat: str/int/float/bool/list[str], no Optional/Union)
# --------------------------------------------------------------------------- #

Paper = Annotated[Literal["A4", "A3", "A5", "Letter", "Legal"], Field(description="Paper size.")]
Landscape = Annotated[bool, Field(description="Landscape orientation.")]
MarginCm = Annotated[float, Field(description="Margin on all sides in centimeters (0-10).")]
PrintBackground = Annotated[bool, Field(description="Print background colors and images.")]
WaitDelay = Annotated[str, Field(description="Wait before rendering, e.g. '2s' or '500ms'. Empty = no delay.")]
OutFilename = Annotated[str, Field(description="Name of the resulting PDF file. Empty = automatic name.")]
ReturnB64 = Annotated[bool, Field(description="Also return the PDF as base64 in the 'base64' field (large!).")]
InputUrl = Annotated[str, Field(description="http(s) URL of the input file, or a download_url/file_id from a previous result.")]
FileB64 = Annotated[str, Field(description="Input file content as base64. Requires source_filename.")]
SourceFilename = Annotated[str, Field(description="Original file name incl. extension, e.g. 'report.docx'.")]
InputPath = Annotated[str, Field(description="Path relative to /data/input inside the server (local setups only).")]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@tool
async def health() -> str:
    """Check the Gotenberg instance: returns its /health status and /version.

    Takes no parameters. Returns JSON with gotenberg_url, health and version.
    """
    try:
        async with _gotenberg_client() as client:
            h = await client.get("/health")
            v = await client.get("/version")
    except httpx.HTTPError as exc:
        raise ToolError(f"Gotenberg not reachable at {GOTENBERG_URL}: {exc.__class__.__name__}: {exc}") from None
    try:
        health_data = h.json()
    except ValueError:
        health_data = h.text[:500]
    return json.dumps(
        {
            "gotenberg_url": GOTENBERG_URL,
            "health_status_code": h.status_code,
            "health": health_data,
            "version": v.text.strip()[:100] if v.status_code == 200 else f"HTTP {v.status_code}",
        }
    )


@tool
async def url_to_pdf(
    url: Annotated[str, Field(description="Public http(s) URL of the web page to render.")],
    paper: Paper = "A4",
    landscape: Landscape = False,
    margin_cm: MarginCm = 1.0,
    print_background: PrintBackground = True,
    wait_delay: WaitDelay = "",
    filename: OutFilename = "",
    return_base64: ReturnB64 = False,
) -> str:
    """Render a web page (URL) to PDF with headless Chromium.

    Args:
        url: Public http(s) URL to render. Internal/private addresses are blocked.
        paper: A4, A3, A5, Letter or Legal.
        landscape: Landscape orientation.
        margin_cm: Margin on all sides in cm.
        print_background: Print background graphics.
        wait_delay: Delay before rendering ('' or e.g. '2s').
        filename: Output file name ('' = derived from URL).
        return_base64: Additionally return the PDF as base64.

    Returns JSON: download_url, filename, bytes, expires_at, trace, file_id (+ base64).
    """
    await _assert_public_url(url)
    form = _chromium_form(paper, landscape, margin_cm, print_background, wait_delay)
    form["url"] = url
    res = await _gotenberg_post("/forms/chromium/convert/url", form, [])
    default = (urlsplit(url).hostname or "page").replace(".", "_")
    return _store_result(res.content, _safe_filename(filename, default, ".pdf"), res.trace, return_base64)


@tool
async def html_to_pdf(
    html_content: Annotated[str, Field(description="Complete HTML document (or fragment) to render.")],
    paper: Paper = "A4",
    landscape: Landscape = False,
    margin_cm: MarginCm = 1.0,
    print_background: PrintBackground = True,
    wait_delay: WaitDelay = "",
    filename: OutFilename = "",
    return_base64: ReturnB64 = False,
) -> str:
    """Render HTML to PDF with headless Chromium.

    Args:
        html_content: HTML source. Use inline CSS/images (data: URIs) or absolute public URLs for assets.
        paper: A4, A3, A5, Letter or Legal.
        landscape: Landscape orientation.
        margin_cm: Margin on all sides in cm.
        print_background: Print background graphics.
        wait_delay: Delay before rendering ('' or e.g. '2s').
        filename: Output file name ('' = 'document.pdf').
        return_base64: Additionally return the PDF as base64.

    Returns JSON: download_url, filename, bytes, expires_at, trace, file_id (+ base64).
    """
    if not html_content.strip():
        raise ToolError("html_content is empty")
    form = _chromium_form(paper, landscape, margin_cm, print_background, wait_delay)
    files = [("files", ("index.html", html_content.encode(), "text/html"))]
    res = await _gotenberg_post("/forms/chromium/convert/html", form, files)
    return _store_result(res.content, _safe_filename(filename, "document", ".pdf"), res.trace, return_base64)


MARKDOWN_WRAPPER = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #222; }}
h1, h2, h3 {{ line-height: 1.25; }}
code, pre {{ font-family: Menlo, Consolas, monospace; font-size: 9.5pt; background: #f4f4f4; }}
pre {{ padding: 8px; overflow-x: auto; white-space: pre-wrap; }}
table {{ border-collapse: collapse; margin: 1em 0; }}
th, td {{ border: 1px solid #999; padding: 4px 8px; text-align: left; }}
th {{ background: #eee; }}
blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 1em; color: #555; }}
img {{ max-width: 100%; }}
{css}
</style>
</head>
<body>
{{{{ toHTML "content.md" }}}}
</body>
</html>
"""


@tool
async def markdown_to_pdf(
    markdown: Annotated[str, Field(description="Markdown text (GitHub-style tables supported).")],
    title: Annotated[str, Field(description="HTML document title. Empty = none.")] = "",
    css: Annotated[str, Field(description="Additional CSS rules appended to the default style.")] = "",
    paper: Paper = "A4",
    landscape: Landscape = False,
    margin_cm: MarginCm = 1.5,
    print_background: PrintBackground = True,
    wait_delay: WaitDelay = "",
    filename: OutFilename = "",
    return_base64: ReturnB64 = False,
) -> str:
    """Render Markdown to PDF (Chromium) using a clean default stylesheet.

    Args:
        markdown: Markdown source text.
        title: Document title ('' = none).
        css: Extra CSS appended to the default stylesheet ('' = none).
        paper: A4, A3, A5, Letter or Legal.
        landscape: Landscape orientation.
        margin_cm: Margin on all sides in cm.
        print_background: Print background graphics.
        wait_delay: Delay before rendering ('' or e.g. '2s').
        filename: Output file name ('' = 'document.pdf').
        return_base64: Additionally return the PDF as base64.

    Returns JSON: download_url, filename, bytes, expires_at, trace, file_id (+ base64).
    """
    if not markdown.strip():
        raise ToolError("markdown is empty")
    if "{{" in css:
        raise ToolError("css must not contain '{{'")
    # html.escape keeps braces; encode them so the title can never form a Go template action
    safe_title = html.escape(title).replace("{", "&#123;").replace("}", "&#125;")
    wrapper = MARKDOWN_WRAPPER.format(title=safe_title, css=css.replace("</", "<\\/"))
    form = _chromium_form(paper, landscape, margin_cm, print_background, wait_delay)
    files = [
        ("files", ("index.html", wrapper.encode(), "text/html")),
        ("files", ("content.md", markdown.encode(), "text/markdown")),
    ]
    res = await _gotenberg_post("/forms/chromium/convert/markdown", form, files)
    return _store_result(res.content, _safe_filename(filename or title, "document", ".pdf"), res.trace, return_base64)


@tool
async def office_to_pdf(
    input_url: InputUrl = "",
    file_base64: FileB64 = "",
    source_filename: SourceFilename = "",
    input_path: InputPath = "",
    pdfa: Annotated[
        Literal["", "PDF/A-1b", "PDF/A-2b", "PDF/A-3b"], Field(description="PDF/A conformance. Empty = regular PDF.")
    ] = "",
    landscape: Landscape = False,
    filename: OutFilename = "",
    return_base64: ReturnB64 = False,
) -> str:
    """Convert an office document (docx, xlsx, pptx, odt, rtf, txt, ...) to PDF with LibreOffice.

    Provide exactly ONE input source:
      - input_url: public http(s) URL (max MAX_INPUT_MB) or a download_url/file_id from a previous result
      - file_base64 + source_filename: file content as base64 plus the original name incl. extension
      - input_path: file below /data/input on the server (local setups only)

    Args:
        input_url: URL of the document.
        file_base64: Document content as base64.
        source_filename: Original file name with extension (required with file_base64).
        input_path: Path relative to /data/input.
        pdfa: '' (regular PDF), 'PDF/A-1b', 'PDF/A-2b' or 'PDF/A-3b'.
        landscape: Landscape orientation.
        filename: Output file name ('' = source name with .pdf).
        return_base64: Additionally return the PDF as base64.

    Returns JSON: download_url, filename, bytes, expires_at, trace, file_id (+ base64).
    """
    content, name = await _load_single_input(input_url, file_base64, source_filename, input_path)
    src_name = _safe_filename(name, "document")
    if "." not in src_name:
        raise ToolError("Cannot determine file type: set source_filename with an extension, e.g. 'report.docx'")
    form = {"landscape": str(landscape).lower()}
    if pdfa:
        form["pdfa"] = pdfa
    files = [("files", (src_name, content, "application/octet-stream"))]
    res = await _gotenberg_post("/forms/libreoffice/convert", form, files)
    out = _safe_filename(filename or Path(src_name).stem, "document", ".pdf")
    return _store_result(res.content, out, res.trace, return_base64)


@tool
async def merge_pdfs(
    urls: Annotated[
        list[str],
        Field(description="PDF sources in order: http(s) URLs, download_url or file_id values from previous results."),
    ] = [],
    input_paths: Annotated[list[str], Field(description="PDF paths relative to /data/input (local setups only).")] = [],
    files_base64: Annotated[list[str], Field(description="PDF contents as base64.")] = [],
    filename: OutFilename = "",
    return_base64: ReturnB64 = False,
) -> str:
    """Merge several PDFs into one, preserving order.

    Order of the merged document: all `urls` (in list order), then `input_paths`,
    then `files_base64`. At least two PDFs in total are required.

    Args:
        urls: http(s) URLs, or download_url / file_id values returned by other gotenberg tools.
        input_paths: Paths relative to /data/input.
        files_base64: Base64-encoded PDFs.
        filename: Output file name ('' = 'merged.pdf').
        return_base64: Additionally return the PDF as base64.

    Returns JSON: download_url, filename, bytes, expires_at, trace, file_id (+ base64).
    """
    count = len(urls) + len(input_paths) + len(files_base64)
    if count < 2:
        raise ToolError("merge_pdfs needs at least two PDFs")
    if count > MAX_MERGE_INPUTS:
        raise ToolError(f"merge_pdfs accepts at most {MAX_MERGE_INPUTS} PDFs")
    limit = MAX_INPUT_BYTES * 4
    sources: list[bytes] = []
    total = 0

    def add(content: bytes) -> None:
        nonlocal total
        total += len(content)
        if total > limit:
            raise ToolError(f"Combined input exceeds {limit // (1024 * 1024)} MB")
        sources.append(content)

    for ref in urls:
        ref = ref.strip()
        own = _own_file_from_ref(ref)
        add(own[0] if own else (await _download(ref))[0])
    for rel in input_paths:
        add(_resolve_input_path(rel).read_bytes())
    for b64 in files_base64:
        add(_decode_b64(b64))
    for i, s in enumerate(sources):
        if not s.startswith(b"%PDF"):
            raise ToolError(f"Input #{i + 1} is not a PDF")
    files = [("files", (f"{i:03d}_input.pdf", s, "application/pdf")) for i, s in enumerate(sources)]
    res = await _gotenberg_post("/forms/pdfengines/merge", {}, files)
    return _store_result(res.content, _safe_filename(filename, "merged", ".pdf"), res.trace, return_base64)


# --------------------------------------------------------------------------- #
# HTTP app: /healthz, /files/{id}, bearer auth for /mcp
# --------------------------------------------------------------------------- #

from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, JSONResponse, Response  # noqa: E402


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/files/{file_id}", methods=["GET"])
async def download_file(request: Request) -> Response:
    file_id = request.path_params["file_id"]
    exp = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")
    if not _verify(file_id, exp, sig):
        return JSONResponse({"error": "invalid or expired signature"}, status_code=403)
    pdf_path, meta_path = _output_paths(file_id)
    if not pdf_path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        name = json.loads(meta_path.read_text()).get("filename", f"{file_id}.pdf")
    except (OSError, ValueError):
        name = f"{file_id}.pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=name, content_disposition_type="inline")


class BearerAuthMiddleware:
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on /mcp (if a token is configured)."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and scope["path"].startswith("/mcp"):
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, value = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value.strip(), self.token):
                resp = JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


if oauth_provider:
    mcp.custom_route("/oauth/login", methods=["GET", "POST"])(oauth_provider.login)


def build_http_app():
    app = mcp.streamable_http_app()
    if oauth_provider:
        # SDK guards /mcp: OAuth access tokens and the static MCP_AUTH_TOKEN
        return app
    if not MCP_AUTH_TOKEN:
        log.warning("MCP_AUTH_TOKEN not set: /mcp is NOT protected")
    return BearerAuthMiddleware(app, MCP_AUTH_TOKEN)


def main() -> None:
    _ensure_dirs()
    threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()
    if TRANSPORT == "stdio":
        log.info("Starting MCP server (stdio), Gotenberg at %s", GOTENBERG_URL)
        mcp.run("stdio")
    elif TRANSPORT in ("streamable-http", "http"):
        import uvicorn

        log.info("Starting MCP server on %s:%d/mcp, Gotenberg at %s", HOST, PORT, GOTENBERG_URL)
        uvicorn.run(
            build_http_app(),
            host=HOST,
            port=PORT,
            log_config=None,
            log_level=LOG_LEVEL.lower(),
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    else:
        log.error("Unknown MCP_TRANSPORT '%s' (use streamable-http or stdio)", TRANSPORT)
        sys.exit(2)


if __name__ == "__main__":
    main()
