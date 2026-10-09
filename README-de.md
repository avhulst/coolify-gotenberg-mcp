# Gotenberg MCP Server

🇬🇧 [English](README.md) | 🇩🇪 **Deutsch**

MCP-Server für [Gotenberg](https://gotenberg.dev) (Dokumentkonvertierung: URL/HTML/Markdown/Office → PDF, PDFs zusammenführen).
Deploybar per **Coolify v4** (Docker-Compose-Buildpack), nutzbar mit **Claude Desktop** und **Mistral Vibe** – remote über HTTPS (`streamable-http`) oder lokal über `stdio`.

- Python 3.12, offizielles MCP Python SDK (`mcp>=1.30,<2`, FastMCP; FastMCP wurde in 2.x umbenannt. Mit einem SDK, das benötigte Einstellungen wie `auth` nicht kennt, bricht der Server den Start ab, statt ungeschützt zu laufen), httpx
- Stateless HTTP (`stateless_http=True`) – robust bei Restarts/Redeploys, keine Session-Bindung an einen Container
- Anmeldung an `/mcp` per **OAuth 2.1** (für Custom Connectors in Claude und Mistral) **oder** festem Bearer-Token (für `mcp-remote`, Vibe-CLI); signierte, ablaufende Download-Links unter `/files/…`
- SSRF-Schutz für `url_to_pdf` und `input_url` (interne IP-Bereiche werden geblockt) plus Gotenberg-Deny-Lists

## Tools

| Tool | Gotenberg-Route | Zweck |
|---|---|---|
| `health` | `GET /health`, `GET /version` | Status und Version der Gotenberg-Instanz |
| `url_to_pdf` | `/forms/chromium/convert/url` | Webseite → PDF |
| `html_to_pdf` | `/forms/chromium/convert/html` | HTML → PDF (wird als `index.html` gesendet) |
| `markdown_to_pdf` | `/forms/chromium/convert/markdown` | Markdown → PDF (Wrapper mit `{{ toHTML "content.md" }}`, Tabellen-Styling) |
| `office_to_pdf` | `/forms/libreoffice/convert` | docx/xlsx/pptx/odt/… → PDF, optional `PDF/A-1b`/`PDF/A-2b`/`PDF/A-3b` |
| `merge_pdfs` | `/forms/pdfengines/merge` | Mehrere PDFs in fester Reihenfolge zusammenführen |

Chromium-Optionen: `paper` (A4/A3/A5/Letter/Legal), `landscape`, `margin_cm`, `print_background`, `wait_delay` (`""` oder z. B. `"2s"`).

Eingaben für `office_to_pdf`: genau **eine** von `input_url` (http/https, max. `MAX_INPUT_MB`, auch `download_url`/`file_id` aus früheren Ergebnissen), `file_base64` + `source_filename`, oder `input_path` (relativ zu `/data/input`, nur lokal sinnvoll).
`merge_pdfs` nimmt `urls` (URLs, `download_url`s oder `file_id`s vorheriger Ergebnisse), `input_paths` und `files_base64` – in dieser Reihenfolge.

Rückgabe jedes Konvertierungs-Tools (JSON):

```json
{
  "download_url": "https://mcp.example.com/files/<id>?exp=1760000000&sig=…",
  "filename": "bericht.pdf",
  "bytes": 14070,
  "expires_at": "2026-10-10T07:12:19+00:00",
  "trace": "f9b36701f657aea6",
  "file_id": "<id>"
}
```

Optional `base64` (bei `return_base64: true`) und `local_path` (wenn `HOST_DATA_DIR` gesetzt ist, z. B. im lokalen stdio-Betrieb).

Für Kompatibilität mit Claude Desktop und Vibe sind die Schemas flach (kein `anyOf`/`null`, nur `str/int/float/bool/list[str]`, Enums als `Literal`), Tool-Namen kurz in `snake_case` (Vibe macht daraus `gotenberg_url_to_pdf` usw.). Fehler kommen als lesbare Meldung zurück (Statuscode + gekürzte Gotenberg-Antwort), ohne Stacktraces.

## Deployment mit Coolify (v4)

1. Dieses Verzeichnis in ein Git-Repo pushen (GitHub/GitLab/Gitea …).
2. In Coolify: **Projects → Environment → + New → Resource**.
3. **Public Repository** (oder *Private Repository (with GitHub App / Deploy Key)*) wählen und die Repo-URL + Branch eintragen.
4. **Build Pack: „Docker Compose“** auswählen, *Docker Compose Location* `/docker-compose.yml` belassen → **Continue**.
5. Coolify erkennt die Services `mcp` und `gotenberg`. Beim Service **`mcp`** unter **Domains** die gewünschte Domain eintragen, z. B. `https://gotenberg-mcp.example.com` (wegen `SERVICE_URL_MCP_8000` routet Traefik automatisch auf Port 8000). `gotenberg` bekommt **keine** Domain und bleibt intern.
6. **Deploy** klicken und warten, bis beide Services *healthy* sind.
7. Unter **Environment Variables** zwei generierte Werte kopieren:
   - **`SERVICE_PASSWORD_64_MCPTOKEN`**: fester Bearer-Token für Clients mit Header-Konfiguration (`mcp-remote`, Vibe-CLI, Mistral „Bearer Token“)
   - **`SERVICE_PASSWORD_OAUTH`**: Passwort für die OAuth-Anmeldeseite (Custom Connectors in Claude und Mistral)
8. Test: `curl https://gotenberg-mcp.example.com/healthz` → `{"status":"ok"}`;
   `curl -i -X POST https://gotenberg-mcp.example.com/mcp` → `401`.

Weitere Werte (`FILE_TTL_HOURS`, `LOG_LEVEL`, `ALLOWED_HOSTS`, `GOTENBERG_URL` …) sind als `${VAR:-default}` referenziert und können in der Coolify-UI geändert werden (danach *Redeploy*).

> **Streaming hängt?** Falls Antworten über HTTPS hängen bleiben oder erst nach Timeout ankommen, in Coolify für die Ressource die **Gzip-Kompression deaktivieren** (Resource → Advanced → „Enable Gzip Compression“ aus). Kompression puffert SSE-Streams.

### Externe Gotenberg-Instanz statt der mitgelieferten

1. In `docker-compose.yml` den Service `gotenberg` und im Service `mcp` den Block `depends_on` entfernen.
2. In Coolify `GOTENBERG_URL` setzen (z. B. `https://gotenberg.example.com`), bei Basic-Auth zusätzlich `GOTENBERG_USERNAME` und `GOTENBERG_PASSWORD`.
3. Redeploy. Mit dem Tool `health` prüfen, ob die Verbindung steht.

Die SSRF-Deny-Lists (siehe *Sicherheit*) müssen dann auf der externen Instanz gesetzt werden.

## Lokaler Betrieb ohne Coolify

```bash
cp .env.example .env          # MCP_AUTH_TOKEN setzen: openssl rand -hex 32
mkdir -p data/input data/output
docker compose -f docker-compose.local.yml up -d --build
curl http://127.0.0.1:8000/healthz
```

- MCP-Endpoint: `http://127.0.0.1:8000/mcp` (nur auf 127.0.0.1 gebunden)
- Ergebnisse unter `./data/output`, Eingaben für `input_path` unter `./data/input`
- Festes Docker-Netzwerk `gotenberg-net` – darüber erreicht ein stdio-Container (`docker run --network gotenberg-net …`) den Gotenberg-Service.
- Das Image heißt `gotenberg-mcp:latest` und wird auch für den stdio-Modus verwendet.
- Linux-Hosts: Der Container läuft als UID 10001. `./data` muss für diese UID beschreibbar sein (`sudo chown -R 10001:10001 data`).

## Clients

### Custom Connectors (Claude Web/Desktop/Mobile, Mistral Le Chat)

Bei Custom Connectors verbindet sich **nicht dein Rechner**, sondern die Cloud des Anbieters (Anthropic: `160.79.104.0/21`). Der Server muss daher öffentlich per HTTPS erreichbar sein – mit Coolify ist das gegeben.

Ablauf (OAuth 2.1 mit Dynamic Client Registration und PKCE, vom SDK bereitgestellt):

1. Der Client ruft `/mcp` ohne Token auf → `401` mit `WWW-Authenticate: Bearer resource_metadata="https://<domain>/.well-known/oauth-protected-resource/mcp"`.
2. Er liest die Metadaten, registriert sich unter `/register` und öffnet `/authorize`.
3. Im Browser erscheint die Anmeldeseite des Servers. Sie zeigt den anfragenden Client und das Weiterleitungsziel (z. B. `claude.ai`). Dort **`SERVICE_PASSWORD_OAUTH`** eingeben → **Erlauben**. Die Seite folgt der Browsersprache (`Accept-Language`: Deutsch oder Englisch, sonst Englisch).
4. Der Client erhält Access-Token (Default 60 min) und Refresh-Token (Default 30 Tage, wird bei jeder Nutzung ersetzt; wird ein bereits benutzter Refresh-Token nach Ablauf der Kulanzzeit erneut vorgelegt, werden alle Tokens dieser Verbindung widerrufen). Die Tokens werden gehasht unter `/data/oauth/state.json` gespeichert und überstehen Restarts/Redeploys.

**Claude** (claude.ai, Claude Desktop, Mobile – Connectors werden im Account synchronisiert):
*Einstellungen → Connectors → Benutzerdefinierten Connector hinzufügen* → Name `Gotenberg`, URL **`https://<domain>/mcp`** (genau so, inkl. `/mcp`) → *Hinzufügen* → *Verbinden* → Anmeldeseite mit `SERVICE_PASSWORD_OAUTH`. Die *Advanced settings* (OAuth Client ID/Secret) bleiben leer. In Team-/Enterprise-Organisationen darf nur ein Owner Custom Connectors hinzufügen.

**Mistral Le Chat** (Connectors → Custom MCP Connector hinzufügen; die genaue Menüführung kann sich ändern):
URL `https://<domain>/mcp`. Als Authentifizierung **OAuth 2.1** wählen (Anmeldung wie oben) oder **HTTP Bearer Token** mit dem Wert von `SERVICE_PASSWORD_64_MCPTOKEN`. Ob die Vibe-CLI im Account angelegte Connectors übernimmt, ist von Mistral nicht dokumentiert – zuverlässig ist die CLI-Konfiguration unten.

Optional lassen sich die erlaubten Weiterleitungsziele mit `OAUTH_REDIRECT_HOSTS` einschränken (z. B. `claude.ai,localhost,127.0.0.1`). Erst nach dem ersten erfolgreichen Verbinden setzen und den Host des Mistral-Callbacks aus den Logs (`Registered OAuth client …`) bzw. der Anmeldeseite übernehmen.


### Claude Desktop

Datei: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json`. Vorlage: [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json).

**a) Remote (Coolify)** – über [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (Node.js nötig):

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

Hinweis: Unter Windows haben manche Versionen Probleme mit Leerzeichen in `args`. Dann `"--header", "Authorization:${AUTH_HEADER}"` und `"env": { "AUTH_HEADER": "Bearer <token>" }` verwenden.

**b) Lokal (stdio via Docker)** – setzt `docker compose -f docker-compose.local.yml up -d --build` voraus:

```json
{
  "mcpServers": {
    "gotenberg_local": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network", "gotenberg-net",
               "-e", "MCP_TRANSPORT=stdio", "-e", "GOTENBERG_URL=http://gotenberg:3000",
               "-e", "HOST_DATA_DIR=/ABSOLUTER/PFAD/zum/repo/data",
               "-v", "/ABSOLUTER/PFAD/zum/repo/data:/data",
               "gotenberg-mcp:latest"]
    }
  }
}
```

Im stdio-Modus gibt es keinen HTTP-Server, daher ist `download_url` leer; stattdessen liefert jedes Ergebnis `local_path` (Pfad auf dem Host) und `file_id` (für `merge_pdfs`).

Claude Desktop nach Änderungen komplett beenden und neu starten.

### Mistral Vibe

Vorlage: [`clients/vibe-config.toml`](clients/vibe-config.toml) → Blöcke nach `./.vibe/config.toml` (Projekt) oder `~/.vibe/config.toml` (global) kopieren.

```toml
# Remote
[[mcp_servers]]
name = "gotenberg"
transport = "streamable-http"
url = "https://<domain>/mcp"
headers = { "Authorization" = "Bearer <token>" }

# Lokal
[[mcp_servers]]
name = "gotenberg_local"
transport = "stdio"
command = "docker"
args = ["run", "-i", "--rm", "--network", "gotenberg-net",
        "-e", "MCP_TRANSPORT=stdio", "-e", "GOTENBERG_URL=http://gotenberg:3000",
        "-e", "HOST_DATA_DIR=/ABSOLUTER/PFAD/zum/repo/data",
        "-v", "/ABSOLUTER/PFAD/zum/repo/data:/data", "gotenberg-mcp:latest"]
startup_timeout_sec = 30
```

Prüfen in Vibe: `/mcp gotenberg_local` (bzw. `/mcp gotenberg`) listet die sechs Tools. Vibe stellt sie als `gotenberg_<tool>` bereit.

## Umgebungsvariablen

| Variable | Default | Beschreibung |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` (Port 8000, `/mcp`) oder `stdio` |
| `MCP_AUTH_TOKEN` | Coolify: `${SERVICE_PASSWORD_64_MCPTOKEN}` | Bearer-Token für `/mcp`. Leer = keine Auth (nur lokal!) |
| `PUBLIC_BASE_URL` | Coolify: `${SERVICE_URL_MCP}` | Basis-URL für Download-Links |
| `ALLOWED_HOSTS` | aus `PUBLIC_BASE_URL` | Kommagetrennte erlaubte `Host`-Header (DNS-Rebinding-Schutz des SDK), z. B. `mcp.example.com,localhost:*`. `*` deaktiviert die Prüfung |
| `FILE_SIGNING_KEY` | = `MCP_AUTH_TOKEN` | Schlüssel für HMAC-SHA256-Signaturen der Download-Links |
| `GOTENBERG_URL` | `http://gotenberg:3000` | Gotenberg-Instanz |
| `GOTENBERG_USERNAME` / `GOTENBERG_PASSWORD` | leer | Basic-Auth für externe Gotenberg-Instanz |
| `GOTENBERG_TIMEOUT` | `120` | Timeout pro Gotenberg-Request (Sekunden) |
| `GOTENBERG_API_TIMEOUT` | `120s` | Nur Compose: `--api-timeout` der mitgelieferten Gotenberg-Instanz |
| `FILE_TTL_HOURS` | `24` | Gültigkeit der Download-Links; ein Hintergrund-Task löscht abgelaufene Dateien |
| `MAX_INPUT_MB` | `50` | Größenlimit für `input_url`/`file_base64`/`input_path` |
| `ALLOW_PRIVATE_URLS` | `false` | `true` erlaubt interne Ziele für `url_to_pdf`/`input_url` (SSRF-Risiko!) |
| `OAUTH_ENABLED` | `true` | OAuth 2.1 aktiv (nur HTTP-Modus mit `PUBLIC_BASE_URL` per https oder localhost) |
| `OAUTH_PASSWORD` | Coolify: `${SERVICE_PASSWORD_OAUTH}`, sonst = `MCP_AUTH_TOKEN` | Passwort der OAuth-Anmeldeseite |
| `OAUTH_ACCESS_TOKEN_TTL_MINUTES` | `60` | Gültigkeit der OAuth-Access-Tokens |
| `OAUTH_REFRESH_TOKEN_TTL_DAYS` | `30` | Gültigkeit der Refresh-Tokens (Rotation bei jeder Nutzung) |
| `OAUTH_REFRESH_REUSE_GRACE_SECONDS` | `10` | Kulanzzeit für parallele Refreshes desselben Clients; danach gilt eine Wiederverwendung als Diebstahl und widerruft die Token-Kette |
| `OAUTH_REDIRECT_HOSTS` | leer (alle) | Kommagetrennte Allowlist für Redirect-Hosts bei der Client-Registrierung |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `HOST_DATA_DIR` | leer | Host-Pfad von `/data`; fügt `local_path` zu Ergebnissen hinzu |
| `DATA_DIR` | `/data` | Datenverzeichnis im Container (`input/`, `output/`) |

## Endpunkte

| Pfad | Auth | Beschreibung |
|---|---|---|
| `POST /mcp` | `Authorization: Bearer <OAuth-Token oder MCP_AUTH_TOKEN>` | MCP Streamable HTTP (stateless) |
| `GET /.well-known/oauth-protected-resource/mcp` | keine | Protected Resource Metadata (RFC 9728) |
| `GET /.well-known/oauth-authorization-server` | keine | Authorization Server Metadata (RFC 8414) |
| `POST /register`, `GET /authorize`, `POST /token`, `POST /revoke` | OAuth | Dynamic Client Registration, Autorisierung (PKCE S256), Token, Widerruf |
| `GET/POST /oauth/login` | Passwort | Anmeldeseite |
| `GET /healthz` | keine | Liveness für Docker/Coolify-Healthcheck |
| `GET /files/{id}?exp=…&sig=…` | HMAC-Signatur | Download eines Ergebnisses; ungültige/abgelaufene Signatur → `403` |

## Sicherheit

- **OAuth**: Die Anmeldeseite ist durch `OAUTH_PASSWORD` geschützt (Vergleich mit `hmac.compare_digest`, 1 s Verzögerung pro Fehlversuch, Sperre nach 10 Fehlversuchen pro IP **und** nach 50 Fehlversuchen insgesamt in 15 min. Die globale Grenze greift auch, wenn ein Angreifer `X-Forwarded-For` fälscht; während der Sperre ist auch das richtige Passwort blockiert) und zeigt das Weiterleitungsziel an – nur erlauben, wenn du die Verbindung gerade selbst eingerichtet hast. Die Seite ist gegen Einbetten geschützt (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`). Tokens werden nur als SHA-256-Hash gespeichert (`/data/oauth/state.json`, Modus 600). Alle Verbindungen trennen: `OAUTH_PASSWORD` ändern und `/data/oauth/state.json` löschen. Gegen Fluten sind registrierte Clients auf 1000 begrenzt (die ältesten ohne gültige Tokens werden verdrängt), offene Anmeldungen auf 500.
- **Auth**: `/mcp` verlangt ein gültiges OAuth-Token oder den festen Bearer-Token (Vergleich mit `hmac.compare_digest`), sonst `401`. `/healthz` und `/files/*` sind ausgenommen; `/files` ist über HMAC-SHA256 (id + exp) signiert und läuft nach `FILE_TTL_HOURS` ab.
- **Host-Prüfung**: Der DNS-Rebinding-Schutz des SDK (`TransportSecuritySettings`) lässt nur Hosts aus `ALLOWED_HOSTS` zu (fremder `Host` → `421`).
- **SSRF**: `url_to_pdf` lässt Gotenbergs Chromium beliebige URLs laden, `input_url` lässt den MCP-Server Dateien herunterladen. Beides kann missbraucht werden, um interne Dienste (Cloud-Metadaten `169.254.169.254`, Docker-Netz, Admin-UIs) zu erreichen.
  - Der MCP-Server löst den Host auf und blockt alle nicht-öffentlichen Adressen (loopback, private, link-local, CGNAT, reserviert, multicast) – auch bei jedem Redirect-Hop von `input_url`. Die Verbindung geht direkt an die geprüfte IP (Host-Header und TLS-Zertifikatsprüfung weiter auf den Hostnamen), damit ein DNS-Rebinding zwischen Prüfung und Abruf nicht greift. URLs mit Zugangsdaten (`user:pass@`) werden abgelehnt.
  - `merge_pdfs` nimmt höchstens 50 Eingaben und bricht ab, sobald die Summe `4 × MAX_INPUT_MB` überschreitet.
  - Zusätzlich startet die mitgelieferte Gotenberg-Instanz mit `--chromium-deny-list` und `--libreoffice-deny-list`, die `file:` (außer Gotenbergs `/tmp`), `localhost`, private/link-local IP-Bereiche, IPv6-Literale und die Service-Namen `mcp`/`gotenberg` sperren. Das wirkt auch für Unterressourcen und Redirects *innerhalb* von Chromium.
  - Rest-Risiko bei `url_to_pdf`: Der MCP-Server prüft die Ziel-URL, aber Chromium in Gotenberg löst den Namen selbst noch einmal auf und folgt Redirects. Hostnamen, die dort auf interne IPs auflösen (DNS-Rebinding, Redirect auf einen solchen Namen), erfasst die Regex-Deny-List nicht. Gotenberg daher in einem Netz ohne Zugriff auf sensible interne Dienste betreiben. Bei einer externen Gotenberg-Instanz die Deny-Lists dort setzen.
- **Path Traversal**: `input_path` wird per `resolve()` + Prefix-Check auf `/data/input` begrenzt.
- **Non-Root**: Der MCP-Container läuft als `app` (UID 10001), `/data` gehört diesem User.
- `stdio`: stdout transportiert ausschließlich JSON-RPC, Logs gehen nach stderr.

## Entwicklung

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
GOTENBERG_URL=http://localhost:3000 DATA_DIR=./data PUBLIC_BASE_URL=http://localhost:8000 python server.py
```

## KI-Unterstützung

Dieses Projekt wurde mit Unterstützung von KI ([Claude Code](https://claude.com/claude-code) von Anthropic) erstellt. Code, Konfiguration und Dokumentation wurden KI-gestützt erzeugt und getestet und von einem Menschen geprüft. Bitte prüfe Code und Sicherheitseinstellungen vor dem produktiven Einsatz.
