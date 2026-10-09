# Gotenberg MCP Server

🇬🇧 **English** | 🇩🇪 [Deutsch](README-de.md)

MCP server for [Gotenberg](https://gotenberg.dev) (document conversion: URL/HTML/Markdown/Office → PDF, merging PDFs).
Deployable via **Coolify v4** (Docker Compose build pack), usable with **Claude Desktop** and **Mistral Vibe**, either remotely over HTTPS (`streamable-http`) or locally over `stdio`.

- Python 3.12, official MCP Python SDK (`mcp>=1.30,<2`, FastMCP; FastMCP was renamed in 2.x). If the installed SDK does not support a required setting such as `auth`, the server refuses to start instead of running unprotected. HTTP client: httpx.
- Stateless HTTP (`stateless_http=True`): survives restarts and redeploys, no session bound to a container
- Authentication on `/mcp` via **OAuth 2.1** (for custom connectors in Claude and Mistral) **or** a static bearer token (for `mcp-remote`, Vibe CLI); signed, expiring download links under `/files/…`
- SSRF protection for `url_to_pdf` and `input_url` (internal IP ranges are blocked) plus Gotenberg deny lists

## Tools

| Tool | Gotenberg route | Purpose |
|---|---|---|
| `health` | `GET /health`, `GET /version` | Status and version of the Gotenberg instance |
| `url_to_pdf` | `/forms/chromium/convert/url` | Web page → PDF |
| `html_to_pdf` | `/forms/chromium/convert/html` | HTML → PDF (sent as `index.html`) |
| `markdown_to_pdf` | `/forms/chromium/convert/markdown` | Markdown → PDF (wrapper with `{{ toHTML "content.md" }}`, table styling) |
| `office_to_pdf` | `/forms/libreoffice/convert` | docx/xlsx/pptx/odt/… → PDF, optionally `PDF/A-1b`/`PDF/A-2b`/`PDF/A-3b` |
| `merge_pdfs` | `/forms/pdfengines/merge` | Merge several PDFs in a fixed order |

Chromium options: `paper` (A4/A3/A5/Letter/Legal), `landscape`, `margin_cm`, `print_background`, `wait_delay` (`""` or e.g. `"2s"`).

Inputs for `office_to_pdf`: exactly **one** of `input_url` (http/https, max. `MAX_INPUT_MB`, also a `download_url`/`file_id` from earlier results), `file_base64` + `source_filename`, or `input_path` (relative to `/data/input`, only useful locally).
`merge_pdfs` takes `urls` (URLs, `download_url`s or `file_id`s from earlier results), `input_paths` and `files_base64`, in that order.

Every conversion tool returns JSON:

```json
{
  "download_url": "https://mcp.example.com/files/<id>?exp=1760000000&sig=…",
  "filename": "report.pdf",
  "bytes": 14070,
  "expires_at": "2026-10-10T07:12:19+00:00",
  "trace": "f9b36701f657aea6",
  "file_id": "<id>"
}
```

Optionally `base64` (with `return_base64: true`) and `local_path` (when `HOST_DATA_DIR` is set, e.g. in local stdio mode).

For compatibility with Claude Desktop and Vibe the schemas are flat (no `anyOf`/`null`, only `str/int/float/bool/list[str]`, enums as `Literal`) and tool names are short `snake_case` (Vibe turns them into `gotenberg_url_to_pdf` etc.). Errors come back as readable messages (status code + truncated Gotenberg response), without stack traces.

## Deploying with Coolify (v4)

1. Push this directory to a Git repository (GitHub/GitLab/Gitea …).
2. In Coolify: **Projects → Environment → + New → Resource**.
3. Choose **Public Repository** (or *Private Repository (with GitHub App / Deploy Key)*) and enter the repository URL and branch.
4. Select **Build Pack: "Docker Compose"**, keep *Docker Compose Location* `/docker-compose.yml` → **Continue**.
5. Coolify detects the services `mcp` and `gotenberg`. For service **`mcp`**, enter the desired domain under **Domains**, e.g. `https://gotenberg-mcp.example.com` (because of `SERVICE_URL_MCP_8000`, Traefik routes to port 8000 automatically). `gotenberg` gets **no** domain and stays internal.
6. Click **Deploy** and wait until both services are *healthy*.
7. Copy two generated values from **Environment Variables**:
   - **`SERVICE_PASSWORD_64_MCPTOKEN`**: static bearer token for clients configured with a header (`mcp-remote`, Vibe CLI, Mistral "Bearer Token")
   - **`SERVICE_PASSWORD_OAUTH`**: password for the OAuth login page (custom connectors in Claude and Mistral)
8. Test: `curl https://gotenberg-mcp.example.com/healthz` → `{"status":"ok"}`;
   `curl -i -X POST https://gotenberg-mcp.example.com/mcp` → `401`.

Further values (`FILE_TTL_HOURS`, `LOG_LEVEL`, `ALLOWED_HOSTS`, `GOTENBERG_URL` …) are referenced as `${VAR:-default}` and can be changed in the Coolify UI (then *Redeploy*).

> **Streaming hangs?** If responses over HTTPS stall or only arrive after a timeout, **disable gzip compression** for the resource in Coolify (Resource → Advanced → turn off "Enable Gzip Compression"). Compression buffers SSE streams.

### External Gotenberg instance instead of the bundled one

1. In `docker-compose.yml`, remove the `gotenberg` service and the `depends_on` block of the `mcp` service.
2. In Coolify, set `GOTENBERG_URL` (e.g. `https://gotenberg.example.com`), plus `GOTENBERG_USERNAME` and `GOTENBERG_PASSWORD` for basic auth.
3. Redeploy. Check the connection with the `health` tool.

The SSRF deny lists (see *Security*) then have to be set on the external instance.

## Running locally without Coolify

```bash
cp .env.example .env          # set MCP_AUTH_TOKEN: openssl rand -hex 32
mkdir -p data/input data/output
docker compose -f docker-compose.local.yml up -d --build
curl http://127.0.0.1:8000/healthz
```

- MCP endpoint: `http://127.0.0.1:8000/mcp` (bound to 127.0.0.1 only)
- Results in `./data/output`, inputs for `input_path` in `./data/input`
- Fixed Docker network `gotenberg-net`: a stdio container (`docker run --network gotenberg-net …`) reaches the Gotenberg service through it.
- The image is named `gotenberg-mcp:latest` and is also used for stdio mode.
- Linux hosts: the container runs as UID 10001. `./data` must be writable for this UID (`sudo chown -R 10001:10001 data`).

## Clients

### Custom connectors (Claude web/desktop/mobile, Mistral Le Chat)

With custom connectors it is **not your computer** that connects but the provider's cloud (Anthropic: `160.79.104.0/21`). The server therefore has to be publicly reachable over HTTPS, which Coolify provides.

Flow (OAuth 2.1 with Dynamic Client Registration and PKCE, provided by the SDK):

1. The client calls `/mcp` without a token → `401` with `WWW-Authenticate: Bearer resource_metadata="https://<domain>/.well-known/oauth-protected-resource/mcp"`.
2. It reads the metadata, registers at `/register` and opens `/authorize`.
3. The server's login page opens in the browser. It shows the requesting client and the redirect target (e.g. `claude.ai`). Enter **`SERVICE_PASSWORD_OAUTH`** there → **Allow**. The page follows the browser language (`Accept-Language`: German or English, fallback English).
4. The client receives an access token (default 60 min) and a refresh token (default 30 days). The refresh token is replaced on every use; if an already used refresh token is presented again after the grace period, all tokens of that connection are revoked. Tokens are stored hashed in `/data/oauth/state.json` and survive restarts and redeploys.

**Claude** (claude.ai, Claude Desktop, mobile; connectors are synced across the account):
*Settings → Connectors → Add custom connector* → name `Gotenberg`, URL **`https://<domain>/mcp`** (exactly like this, including `/mcp`) → *Add* → *Connect* → login page with `SERVICE_PASSWORD_OAUTH`. Leave the *Advanced settings* (OAuth Client ID/Secret) empty. In Team/Enterprise organizations only an Owner can add custom connectors.

**Mistral Le Chat** (Connectors → add a custom MCP connector; the exact menu path may change):
URL `https://<domain>/mcp`. Choose **OAuth 2.1** as authentication (login as above) or **HTTP Bearer Token** with the value of `SERVICE_PASSWORD_64_MCPTOKEN`. Mistral does not document whether the Vibe CLI picks up connectors created in the account; the CLI configuration below is the reliable way.

Optionally, restrict the allowed redirect targets with `OAUTH_REDIRECT_HOSTS` (e.g. `claude.ai,localhost,127.0.0.1`). Set it only after the first successful connection, and take the Mistral callback host from the logs (`Registered OAuth client …`) or from the login page.

### Claude Desktop

File: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json`. Template: [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json).

**a) Remote (Coolify)** via [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (requires Node.js):

```json
{
  "mcpServers": {
    "gotenberg": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<domain>/mcp", "--header", "Authorization: Bearer ${MCP_TOKEN}"],
      "env": { "MCP_TOKEN": "<SERVICE_PASSWORD_64_MCPTOKEN>" }
    }
  }
}
```

Note: on Windows some versions have trouble with spaces in `args`. In that case use `"--header", "Authorization:${AUTH_HEADER}"` with `"env": { "AUTH_HEADER": "Bearer <token>" }`.

**b) Local (stdio via Docker)**, requires `docker compose -f docker-compose.local.yml up -d --build`:

```json
{
  "mcpServers": {
    "gotenberg_local": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network", "gotenberg-net",
               "-e", "MCP_TRANSPORT=stdio", "-e", "GOTENBERG_URL=http://gotenberg:3000",
               "-e", "HOST_DATA_DIR=/ABSOLUTE/PATH/to/repo/data",
               "-v", "/ABSOLUTE/PATH/to/repo/data:/data",
               "gotenberg-mcp:latest"]
    }
  }
}
```

In stdio mode there is no HTTP server, so `download_url` is empty; instead every result contains `local_path` (path on the host) and `file_id` (for `merge_pdfs`).

Quit Claude Desktop completely and restart it after changes.

### Mistral Vibe

Template: [`clients/vibe-config.toml`](clients/vibe-config.toml) → copy the blocks to `./.vibe/config.toml` (project) or `~/.vibe/config.toml` (global).

```toml
# Remote
[[mcp_servers]]
name = "gotenberg"
transport = "streamable-http"
url = "https://<domain>/mcp"
headers = { "Authorization" = "Bearer <token>" }

# Local
[[mcp_servers]]
name = "gotenberg_local"
transport = "stdio"
command = "docker"
args = ["run", "-i", "--rm", "--network", "gotenberg-net",
        "-e", "MCP_TRANSPORT=stdio", "-e", "GOTENBERG_URL=http://gotenberg:3000",
        "-e", "HOST_DATA_DIR=/ABSOLUTE/PATH/to/repo/data",
        "-v", "/ABSOLUTE/PATH/to/repo/data:/data", "gotenberg-mcp:latest"]
startup_timeout_sec = 30
```

Check in Vibe: `/mcp gotenberg_local` (or `/mcp gotenberg`) lists the six tools. Vibe exposes them as `gotenberg_<tool>`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` (port 8000, `/mcp`) or `stdio` |
| `MCP_AUTH_TOKEN` | Coolify: `${SERVICE_PASSWORD_64_MCPTOKEN}` | Bearer token for `/mcp`. Empty = no auth (local only!) |
| `PUBLIC_BASE_URL` | Coolify: `${SERVICE_URL_MCP}` | Base URL for download links |
| `ALLOWED_HOSTS` | derived from `PUBLIC_BASE_URL` | Comma-separated allowed `Host` headers (the SDK's DNS rebinding protection), e.g. `mcp.example.com,localhost:*`. `*` disables the check |
| `FILE_SIGNING_KEY` | = `MCP_AUTH_TOKEN` | Key for the HMAC-SHA256 signatures of download links |
| `GOTENBERG_URL` | `http://gotenberg:3000` | Gotenberg instance |
| `GOTENBERG_USERNAME` / `GOTENBERG_PASSWORD` | empty | Basic auth for an external Gotenberg instance |
| `GOTENBERG_TIMEOUT` | `120` | Timeout per Gotenberg request (seconds) |
| `GOTENBERG_API_TIMEOUT` | `120s` | Compose only: `--api-timeout` of the bundled Gotenberg instance |
| `FILE_TTL_HOURS` | `24` | Validity of download links; a background task deletes expired files |
| `MAX_INPUT_MB` | `50` | Size limit for `input_url`/`file_base64`/`input_path` |
| `ALLOW_PRIVATE_URLS` | `false` | `true` allows internal targets for `url_to_pdf`/`input_url` (SSRF risk!) |
| `OAUTH_ENABLED` | `true` | OAuth 2.1 on (HTTP mode only, with `PUBLIC_BASE_URL` on https or localhost) |
| `OAUTH_PASSWORD` | Coolify: `${SERVICE_PASSWORD_OAUTH}`, otherwise = `MCP_AUTH_TOKEN` | Password of the OAuth login page |
| `OAUTH_ACCESS_TOKEN_TTL_MINUTES` | `60` | Validity of OAuth access tokens |
| `OAUTH_REFRESH_TOKEN_TTL_DAYS` | `30` | Validity of refresh tokens (rotated on every use) |
| `OAUTH_REFRESH_REUSE_GRACE_SECONDS` | `10` | Grace period for parallel refreshes by the same client; after it, reuse counts as theft and revokes the token chain |
| `OAUTH_REDIRECT_HOSTS` | empty (all) | Comma-separated allowlist of redirect hosts for client registration |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `HOST_DATA_DIR` | empty | Host path of `/data`; adds `local_path` to results |
| `DATA_DIR` | `/data` | Data directory in the container (`input/`, `output/`) |

## Endpoints

| Path | Auth | Description |
|---|---|---|
| `POST /mcp` | `Authorization: Bearer <OAuth token or MCP_AUTH_TOKEN>` | MCP Streamable HTTP (stateless) |
| `GET /.well-known/oauth-protected-resource/mcp` | none | Protected Resource Metadata (RFC 9728) |
| `GET /.well-known/oauth-authorization-server` | none | Authorization Server Metadata (RFC 8414) |
| `POST /register`, `GET /authorize`, `POST /token`, `POST /revoke` | OAuth | Dynamic Client Registration, authorization (PKCE S256), token, revocation |
| `GET/POST /oauth/login` | password | Login page |
| `GET /healthz` | none | Liveness for the Docker/Coolify healthcheck |
| `GET /files/{id}?exp=…&sig=…` | HMAC signature | Download of a result; invalid/expired signature → `403` |

## Security

- **OAuth**: The login page is protected by `OAUTH_PASSWORD` (compared with `hmac.compare_digest`, 1 s delay per failed attempt, lockout after 10 failures per IP **and** after 50 failures in total within 15 min). The global limit also applies when an attacker spoofs `X-Forwarded-For`; during a lockout even the correct password is blocked. The page shows the redirect target; only allow access if you just set up the connection yourself. It cannot be embedded in other pages (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`). Tokens are stored only as SHA-256 hashes (`/data/oauth/state.json`, mode 600). To disconnect all clients, change `OAUTH_PASSWORD` and delete `/data/oauth/state.json`. Against flooding, registered clients are capped at 1000 (the oldest without valid tokens are evicted) and pending logins at 500.
- **Auth**: `/mcp` requires a valid OAuth token or the static bearer token (compared with `hmac.compare_digest`), otherwise `401`. `/healthz` and `/files/*` are exempt; `/files` is signed with HMAC-SHA256 (id + exp) and expires after `FILE_TTL_HOURS`.
- **Host check**: The SDK's DNS rebinding protection (`TransportSecuritySettings`) only accepts hosts from `ALLOWED_HOSTS` (foreign `Host` → `421`).
- **SSRF**: `url_to_pdf` lets Gotenberg's Chromium load arbitrary URLs, `input_url` lets the MCP server download files. Both can be abused to reach internal services (cloud metadata `169.254.169.254`, the Docker network, admin UIs).
  - The MCP server resolves the host and blocks every non-public address (loopback, private, link-local, CGNAT, reserved, multicast), also on every redirect hop of `input_url`. The connection goes directly to the checked IP (Host header and TLS certificate check still use the hostname), so DNS rebinding between check and fetch does not work. URLs with credentials (`user:pass@`) are rejected.
  - `merge_pdfs` accepts at most 50 inputs and aborts as soon as their total exceeds `4 × MAX_INPUT_MB`.
  - In addition, the bundled Gotenberg instance starts with `--chromium-deny-list` and `--libreoffice-deny-list`, which block `file:` (except Gotenberg's `/tmp`), `localhost`, private/link-local IP ranges, IPv6 literals and the service names `mcp`/`gotenberg`. This also applies to subresources and redirects *inside* Chromium.
  - Residual risk for `url_to_pdf`: the MCP server checks the target URL, but Chromium in Gotenberg resolves the name again by itself and follows redirects. Hostnames that resolve to internal IPs there (DNS rebinding, a redirect to such a name) are not caught by the regex deny list. Run Gotenberg in a network without access to sensitive internal services. With an external Gotenberg instance, set the deny lists there.
- **Path traversal**: `input_path` is confined to `/data/input` via `resolve()` + prefix check.
- **Non-root**: The MCP container runs as `app` (UID 10001); `/data` belongs to this user.
- `stdio`: stdout carries JSON-RPC only, logs go to stderr.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
GOTENBERG_URL=http://localhost:3000 DATA_DIR=./data PUBLIC_BASE_URL=http://localhost:8000 python server.py
```

## AI assistance

This project was created with the help of AI ([Claude Code](https://claude.com/claude-code) by Anthropic). Code, configuration and documentation were generated and tested with AI support and reviewed by a human. Please review the code and the security settings before using it in production.
