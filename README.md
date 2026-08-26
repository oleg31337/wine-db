# wine-db

A self-hosted, private database of wines you've tasted. Scan a bottle's label
with your phone camera, let a local vision model read it, and the app fills in
the card for you. Rate, comment, group into favorites, and search your cellar —
all behind a reverse proxy on your own hardware.

- **Private & local-first** — your data stays on your server; AI runs on your
  Ollama (or any OpenAI-compatible endpoint).
- **Label scanning** — front *and* back label OCR, barcode lookup, and web
  enrichment, merged into one card.
- **Secure** — Argon2id passwords, HttpOnlyJWT cookies, CSRF protection,
  per-IP rate limiting, safe image re-encoding (EXIF stripped), strict CSP.
- **Admin-gated management** — no public sign-up; one admin (seeded from env)
  creates users and manages backups.
- **Resilient** — never destroys the database on restart; host-mounted `./data`.

---

## Quick start (Docker)

```bash
cp .env.example .env
# Generate the two secrets and paste them into .env:
openssl rand -base64 48      # -> SECRET_KEY
openssl rand -base64 24      # -> POSTGRES_PASSWORD
# Create the admin password hash and paste it into .env:
python -m app.tools.admin_hash "your-admin-password"   # -> ADMIN_PASSWORD_HASH
docker compose up -d --build
```

The app serves on `0.0.0.0:8080` inside the container. Put it behind a reverse
proxy (nginx/Caddy) that terminates TLS — the app speaks plain HTTP and relies
on the proxy for HTTPS, `client_max_body_size`/`max_body_size`, and
authentication offload (none here; auth is app-level).

> **Web cam note:** `getUserMedia` only works on a *secure origin* (HTTPS, or
> `localhost`/`127.0.0.1`). Over a plain-HTTP LAN address the live camera is
> disabled — use **"Choose a photo"** (the OS camera picker) instead, which
> still works.

Open the app through your HTTPS URL, sign in as the admin, and create user
accounts from the **Data** tab.

---

## Configuration

All backend configuration lives in `.env` (see `.env.example`). The web
interface never sees or sets any of it. Highlights:

| Setting | Purpose |
| --- | --- |
| `SECRET_KEY` | Signs session cookies (≥32 chars). |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` | Seeds the only admin account. Production refuses to boot without them. |
| `OLLAMA_BASE_URL` / `VISION_MODEL` | Local vision model that reads labels. |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | Optional OpenAI-compatible endpoint (wins over Ollama when set). |
### Web search & enrichment
Internet lookups run a general web search and, if that returns nothing (DuckDuckGo's
unauthenticated HTML endpoint is increasingly gated by an anti-bot challenge),
fall back to **Wikipedia's keyless API** for real encyclopedic wine facts. A
`WEB_SEARCH_PROVIDER=searxng` + `SEARXNG_BASE_URL` setup uses your own instance
instead. Set `WEB_SEARCH_ENABLED=false` to disable web enrichment entirely. A
`site:saq.com` scoped query is also attempted, but SAQ has no public API, so it
only contributes when the primary engine is reachable.

| `WEB_SEARCH_ENABLED` | Enriches wines with web data (DuckDuckGo → Wikipedia fallback, or SearXNG). |
| `WEB_SEARCH_PROVIDER` | `duckduckgo` (default) or `searxng`. |
| `SEARXNG_BASE_URL` | Your self-hosted SearXNG instance (used when provider is `searxng`). |
| `MAX_UPLOAD_BYTES` | Photo ceiling (default 8 MiB). |
| `DATA_DIR` / `PG_DATA_DIR` | Host folders for app state and the database — **back these up**. |

Generate the admin hash with:

```bash
python -m app.tools.admin_hash "your-admin-password"
```

---

## Scanning a bottle

1. Tap **Scan** and point the camera at the **front label** (or choose a photo).
2. The app resizes the photo in your browser, sends it to the vision model, and
   pre-fills the card with what it read.
3. **Always offered:** a *"Scan the back label"* button. The back label usually
   carries the region, grape, alcohol and sugar that the front omits. Scanning
   it **merges** its data into the *same* card (front data is never lost) — it
   does not start over. This works in both automatic (camera) and manual add
   modes.
4. Review the merged card, fill anything missing, and **Save wine**.

The raw back-label text is stored with the wine as provenance; the structured
facts (grape, region, country, alcohol, sugar) are merged into the fields.

- **Structure gauges (Acidity, Sweetness, Body, Mouthfeel, Wood/Oak)** use a
  simple **0–3 scale**, shown as little progress bars on the card. `0` means
  *"no such taste"* (e.g. a dry, unoaked wine); leave it blank to mark the
  trait as *not assessed*. The scan pipeline tries to fill these from the label
  **and from a web search** — if the web summariser describes the wine as
  *"full-bodied"* or *"oaked"*, those words are mapped onto the 0–3 scale
  automatically. You can always adjust them on the card.

### Front label is the wine photo
When you scan the **front and back** labels, only the **front** label photo is
saved as the wine's picture. The back label is read for its text (region,
grape, alcohol, sugar) and merged into the card, but its image is never stored
as the wine photo.

### Web search includes SAQ
A **`site:saq.com`** scoped query is also sent to the search engine so SAQ's
product pages are considered when the engine is reachable. SAQ has no public API,
so if the primary engine (DuckDuckGo/SearXNG) is unavailable, that query simply
contributes nothing and the general Wikipedia fallback still enriches the wine.

### How images are handled

- **One canonical format & resolution.** *Every* image that enters the system —
  uploaded from a phone, pasted, or fetched from a web page — is re-encoded
  through Pillow to **JPEG** at a single maximum resolution per role:
    - **AI copy** (≤1600px) is sent to the vision model for accurate label OCR.
    - **Stored copy** (≤800px) is persisted and shown in the ~260px detail
      frame and the 62×82 mini-card.
  Because both copies are always JPEG, **all images stored in the database
  share the same format and the same maximum resolution**, regardless of whether
  they arrived as JPEG, PNG, WebP, AVIF, GIF, BMP, TIFF, HEIC, ICO, or MPO.
  Alpha/transparency is flattened and EXIF is stripped on the way through.
- **In the browser:** photos — label scans *and* manual uploads on the wine
  card — are downscaled (≤1600px JPEG) *before* upload so they clear the
  reverse proxy's request-body limit and upload fast. (If the browser can't
  resize, the original is sent, so the proxy limit below is your safety net.)
- **Sent to the AI:** the high-resolution JPEG copy above.
- **Stored on disk:** the smaller display copy. All images are re-encoded
  through Pillow, which rejects non-image payloads (SVG/HTML/polyglots).

---

## Accounts & permissions

- There is **no public registration**. The admin is the only account that can
  create/remove users and manage database import/export.
- **Non-admin users** can add wines, rate, comment, and manage favorites, and
  change their *own* password (gear ⚙ → *Change password*). They do **not** get
  the Data/backup/user-management screens.
- **Admins** can reset any user's password from the user list (invalidates that
  user's existing sessions) and export/import the whole database as a ZIP.
- New accounts are **not** forced to change the initial password.

---

## Backup & restore

Admin → **Data** tab → **Export** downloads a ZIP (wines, ratings, comments,
photos, users). **Import** can *replace* or *merge* into the current database.
Backups are admin-only.

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt requirements-dev.txt
pytest                 # or: scripts/run_tests.sh  (CI-parity, isolated per file)
```

The backend is FastAPI + SQLAlchemy (SQLite for dev, Postgres in Docker).
Frontend is vanilla JS under `static/` (no build step). Run the dev server with
`uvicorn app.main:app --reload` (set `ENVIRONMENT=development` and a dev
`SECRET_KEY`/`ADMIN_PASSWORD_HASH`, or let it auto-generate).

---

## Deploy notes

- Terminate TLS at the reverse proxy; the app expects plain HTTP behind it.
- Raise the proxy body limit to comfortably exceed `MAX_UPLOAD_BYTES`
  (e.g. nginx `client_max_body_size 12m;` or Caddy `max_body_size 12M`) — the
  app already shrinks photos client-side, but the limit is a safety net.
- `data/` and `pgdata/` are host state — never commit them (see `.gitignore`).
  Back them up regularly.
- The container runs as a non-root user with a read-only root filesystem,
  dropped capabilities, and bind-mounted `./data` + `./pgdata`.

---

## Security model

- Passwords hashed with **Argon2id**; sessions are JWT in HttpOnly,
  SameSite cookies; CSRF tokens on every state-changing request.
- Per-IP rate limiting on login, API, and AI endpoints.
- Uploaded images are validated and re-encoded server-side (EXIF stripped,
  non-images rejected, polyglot/SVG blocked, path traversal refused).
- `ENVIRONMENT=production` refuses to boot with a placeholder `SECRET_KEY` or
  missing admin credentials.
