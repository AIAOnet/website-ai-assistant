# Website AI Assistant

A self-hosted proof of concept that learns from a public website and provides a grounded AI assistant, embeddable chat widget, administration workspace, and protected server API.

## Included components

- Bounded public website crawler with robots.txt and network-scope protections
- Source extraction, versioned knowledge builds, and rollback
- English and Swedish retrieval with citations
- Optional OpenAI-compatible generation and embedding providers
- Configurable RAG, sources, ontology, evaluations, routing rules, and logs
- Public browser widget with short-lived, origin-bound visitor tokens
- Private API protected by an administrator-generated bearer token
- Shared persistent rate limits and AI usage budgets
- Simulated appointment calendar for demonstration workflows
- Docker Compose and local Python startup options

## Quick start with Docker

Requirements: Docker Desktop with Docker Compose.

```powershell
git clone https://github.com/AIAOnet/website-ai-assistant.git
cd website-ai-assistant
Copy-Item .env.example .env
docker compose build
docker compose run --rm --no-deps app python -m site_runtime.admin_auth
```

On macOS or Linux, replace the copy command with:

```bash
cp .env.example .env
```

The hash command securely prompts for a password of at least 12 characters. Copy its complete output value into `.env`, then configure:

```dotenv
WEBSITE_ASSISTANT_HOST=0.0.0.0
WEBSITE_ASSISTANT_ADMIN_USERNAME=admin
WEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH='pbkdf2_sha256$600000$...'
```

Keep the hash quoted because it contains `$` characters. Start the application:

```powershell
docker compose up -d
```

Open the assistant at http://127.0.0.1:8001 and administration at http://127.0.0.1:8001/admin.

No administrator password or provider key is supplied by default. Keep the application bound to localhost for the proof of concept. Use HTTPS, secure cookies, and appropriate network controls before making it remotely accessible.

## Create the administrator hash locally

Python 3.13 can generate the hash without Docker:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m site_runtime.admin_auth
```

On macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m site_runtime.admin_auth
```

Copy the generated value into `WEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH` in `.env`. Changing the configured username, hash, or role revokes existing administrator sessions.

## Build website knowledge

1. Sign in at `/admin`.
2. Open **Knowledge Sources → Import website**.
3. Enter a public homepage URL and start the knowledge build.
4. Review the discovery report, extracted sources, and ontology.
5. Ask questions in the built-in assistant or install the generated widget.

A successful build becomes the selected dataset automatically unless validation requires review or automatic activation is disabled. Failed or empty builds preserve the current dataset, and earlier builds can be restored.

The crawler supports public server-rendered HTML. It does not render JavaScript-only pages, sign into websites, or bypass robots.txt. Scraped content never authorizes appointment contacts.

## Configure AI providers

Generation and embeddings are optional. Without them, the assistant uses cited source excerpts and lexical retrieval.

Configure providers in **Admin â†’ AI Settings**, or set the corresponding placeholders in `.env`:

```dotenv
WEBSITE_ASSISTANT_AI_API_ENDPOINT=https://provider.example/v1/chat/completions
WEBSITE_ASSISTANT_AI_API_KEY='your-private-key'
WEBSITE_ASSISTANT_AI_MODEL=your-model

WEBSITE_ASSISTANT_EMBEDDING_ENDPOINT=https://provider.example/v1/embeddings
WEBSITE_ASSISTANT_EMBEDDING_API_KEY='your-private-key'
WEBSITE_ASSISTANT_EMBEDDING_MODEL=your-embedding-model
```

Use the connection tests before rebuilding embeddings. AI usage limits are configured in the same page and persist across restarts. Generation is counted in provider calls and embeddings in submitted input texts; these limits are safeguards rather than exact token or currency accounting.

## Embed the public widget

In **Admin â†’ Website & Integrations**, add each exact allowed website origin and copy the generated widget snippet. The widget requests a short-lived visitor token and never contains the private API token.

For a local cross-origin test, keep the assistant on port 8001, allow `http://127.0.0.1:8002`, and run:

```powershell
.\.venv\Scripts\python.exe -m http.server 8002 --bind 127.0.0.1 --directory examples/widget-test
```

Then open http://127.0.0.1:8002. Public widgets are anonymous by design. Origin checks, signed visitor sessions, persistent rate limits, and AI usage budgets reduce abuse but do not authenticate end users.

## Use the private API

Open **Admin â†’ Website & Integrations â†’ Private API**, generate a private API token, and copy it when displayed. Only its hash is stored. Rotation immediately invalidates the previous token.

```http
POST /api/chat HTTP/1.1
Authorization: Bearer <private-token>
Content-Type: application/json

{"conversation_id":"integration-session-1","message":"What services are available?","language":"en"}
```

The response includes `answer`, `sources`, `grounding`, and `generation`. Private availability and appointment routes use the same bearer credential. Keep the token on the integrating server and use HTTPS outside local development. Interactive request schemas are available at `/docs`; `/healthz` remains public.

## Calendar demonstration

The calendar is deterministic and local. It can demonstrate availability, booking, rescheduling, cancellation, collision protection, and session ownership, but it does not connect to an external calendar or send invitations. Approved contacts must be configured before appointment controls are available.

## Persistence and clean installation

Runtime state is created under the ignored `data/` directory. Every clone starts with fresh databases, no website knowledge, no API token, no administrator sessions, and no provider credentials.

Back up `.env` and `data/` to preserve an installation. Never commit or distribute either one. The Docker configuration mounts `.env` read-only and keeps `data/` on the host.

## Verify the installation

```powershell
docker compose ps
docker compose logs --tail 100 app
```

The container should be running on `127.0.0.1:8001`. A health check should return `{"status":"healthy","demo":true}`:

```powershell
Invoke-WebRequest http://127.0.0.1:8001/healthz
```

## Manage the container

```powershell
# Follow logs
docker compose logs -f app

# Rebuild after application or dependency changes
docker compose up -d --build

# Stop without deleting runtime data
docker compose down
```

To reset the proof of concept, stop it and remove the local `data/` directory. This permanently deletes knowledge builds, settings, API access, appointments, audit records, and usage counters.

## Run the tests

Install the development dependencies and run the isolated test suite:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

On macOS or Linux, use `.venv/bin/python`. The current suite contains 166 tests covering authentication, API access, crawling, extraction, knowledge activation and rollback, grounding, language handling, ontology review, routing, provider safety, rate limits, and AI usage budgets.

## Proof-of-concept scope

This repository demonstrates the complete local workflow and security boundaries. Production deployment still requires an HTTPS reverse proxy, secure administrator cookies, operational monitoring, backups, provider billing controls, and deployment-specific authentication for private API consumers. Multi-host deployments require networked shared rate limiting and usage accounting instead of SQLite files.

## License

This project is available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md). You may use, modify, and distribute it for permitted noncommercial purposes. Commercial use requires a separate license from the repository owner.
