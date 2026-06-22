# Blackbox Brain — Secure Terminal Stack

A three-service stack:

```
┌─────────────┐     /api/*      ┌─────────────┐     /api/chat    ┌─────────────┐
│   console    │ ───────────────▶│   gateway    │ ─────────────────▶│   Ollama     │
│ React+TS UI  │  (nginx proxy)  │  FastAPI     │  (httpx stream)   │ (external)   │
│ "CIA console"│◀─────────────── │  + Postgres  │◀─────────────────  │             │
└─────────────┘   NDJSON stream  └──────┬───────┘                   └─────────────┘
                                          │
                                   ┌──────▼───────┐
                                   │  postgres    │
                                   └──────────────┘
```

- **`gateway/`** — the hardened FastAPI auth + streaming proxy in front of
  Ollama (see `gateway/README.md` for its own security notes).
- **`agent/`** — a React + TypeScript single-page console styled as a
  sci-fi mission-control terminal. It authenticates against the gateway,
  streams `/api/chat` responses token-by-token, and visualizes the
  Ingestion → Orchestration → Swarm → Consolidation workflow from the
  Blackbox Brain design as an animated pipeline diagram. It also has a
  **WORKSPACE** tab for managing project files (create/delete projects,
  list/upload/delete files, download a project as a zip).
- **`docker-compose.yml`** — wires all three together on an isolated
  internal Docker network, plus a named volume for persistent project
  file storage. Only the console's nginx is published to the host;
  Postgres and the gateway are reachable only from inside the network.

## Default model: qwen2.5-coder:0.5b

The console now defaults to `qwen2.5-coder:0.5b` (override with
`OLLAMA_MODEL` in `.env`, or `VITE_OLLAMA_MODEL` for a non-Docker dev
build) and prepends a system prompt tuned specifically for that model's
size — see `agent/src/lib/systemPrompt.ts` for the prompt itself and the
reasoning behind each rule. In short: 0.5B-class models lose track of
long, prose-style instructions and tend to over-explain by default, so
the prompt uses short imperative rules, one worked example, a hard output
template (fenced code block, no commentary unless asked), and an explicit
"say UNKNOWN rather than invent an API" instruction — all things that
matter much more at 0.5B scale than at larger sizes where instructions
can be looser.

Pull the model before first use: `ollama pull qwen2.5-coder:0.5b`.

## Project workspace

The WORKSPACE tab (next to BRAIN WORKFLOW in the right-hand panel) talks
to the gateway's new project/file endpoints:

- Create / delete projects
- List files in a project (recursive, with size + modified time)
- Upload a file into a project
- Delete a single file
- Download the whole project as a `.zip`

Full endpoint reference and curl examples: `gateway/README.md` →
"Project workspace API". Files are stored on a named Docker volume
(`workspaces`), validated against directory traversal on every request,
and capped at 20 MB/file and 200 MB/project by default
(`MAX_UPLOAD_BYTES` / `MAX_PROJECT_BYTES`).

## Run it

```bash
cp .env.example .env
# edit .env — set POSTGRES_PASSWORD, SEED_ADMIN_PASSWORD, ALLOWED_ORIGINS,
# and OLLAMA_BASE_URL (use host.docker.internal if Ollama runs on your host)

docker compose up -d --build
```

Or use the helper script, which checks for Docker/Compose, scaffolds
`.env` from the example on first run, and prints the console URL:

```bash
chmod +x deploy.sh   # already executable in the packaged tarball
./deploy.sh up        # build and start everything
./deploy.sh status     # check container health
./deploy.sh logs        # tail logs from all services
./deploy.sh down        # stop containers (keeps data volumes)
./deploy.sh down -v      # stop containers and DELETE all data volumes
```

Open `http://localhost:8088` (or whatever `CONSOLE_PORT` you set).

Log in with `admin` / the `SEED_ADMIN_PASSWORD` you configured.

> No Docker daemon is available in the environment this was built in, so
> the build itself could not be executed here. Both the gateway and the
> console were validated independently:
> - gateway: `py_compile`, clean import, `pyflakes` (0 issues), `bandit`
>   (0 issues) — see `gateway/README.md`.
> - console: `tsc -b` (clean, strict mode) and `vite build` (succeeds,
>   ~50 KB gzipped JS) — see below.
>
> Run `docker compose build` yourself as the final gate before deploying.

## How the console maps to the Blackbox Brain design

The gateway only exposes a single `/api/chat` call — there's no separate
classifier/planner/swarm API on the backend. So the console does the
routing client-side, the same way the design doc's "Gateway & Router"
component would, and uses it purely to decide **which pipeline stages to
light up** while the one real request streams in:

| Diagram stage | Console behavior |
|---|---|
| Classifier | Heuristic intent check (`chat` vs `task`) run on the input text before sending |
| Orchestrator / Planner | Shown only for `task`-classified input; cosmetic stage transition |
| Swarm Execution | Three synthetic "worker" lanes animate to illustrate parallel ReAct execution (`max_loop = 3`, matching the design doc) — replaced with the model's own reported actions if it emits telemetry (see below) |
| Consolidation | Final stage before the streamed response is marked complete |

The actual assistant text always comes from the one real, authenticated,
streamed call to the gateway — the workflow diagram is an honest
visualization of *intent*, not a second backend doing real planning. If
you build out real planner/swarm endpoints later, swap the timed
animation in `agent/src/App.tsx` (`runSwarmAnimation`) for actual
server-sent stage events.

## Telemetry (TELEMETRY tab)

For task-classified messages, the system prompt asks the model to emit a
fenced ```` ```telemetry ```` JSON block — a plan (orchestrator) and a list
of `{task, reason, command}` actions (swarm workers) — before its actual
answer. The console parses that block and shows it in the TELEMETRY tab.

**This is the model describing what it's doing, not a sandbox executing
commands and reporting real results.** Nothing is verified against actual
execution — there is no command runner anywhere in this stack. The panel
is labeled "AGENT-REPORTED" for exactly this reason; treat it as a
structured rationale/trace from the model, not an audit log. See
`agent/src/lib/systemPrompt.ts` (`TASK_MODE_TELEMETRY_ADDENDUM`) for the
exact instructions given to the model, and `agent/src/lib/telemetry.ts`
for the parser — it fails silently (zero events, no crash) if the model
doesn't comply with the format, which `qwen2.5-coder:0.5b` won't always
do at that size.

## Workspace auto-save

Pick an active project in the WORKSPACE tab (or via the bar above the
chat input). When one is selected, any fenced code block the model tags
with an explicit path — ```` ```python:src/app.py ```` instead of plain
```` ```python ```` — is automatically uploaded into that project via the
gateway's existing `/api/projects/{name}/files` endpoint. Untagged code
blocks are left as in-chat snippets only; the model has to opt in
per-file, so nothing is written to disk unless it explicitly names a
path. Paths are checked client-side against directory traversal before
upload, and re-checked server-side by the gateway regardless (the
client-side check is a UX nicety, not the security boundary).

## Themes

The header has a theme selector — PHOSPHOR (default), CYBERPUNK, MATRIX,
TRON GRID, and AMBER RETRO. Each is a CSS custom-property override in
`agent/src/styles/themes.css`; every other stylesheet reads colors via
`var(--phosphor)` etc., so adding a new theme is just adding another
`[data-theme="..."]` block there — no component changes needed. The
selection persists in `localStorage` (this is a real deployed app, not a
sandboxed artifact, so that's the appropriate place for it).

## Console security notes

- The bearer token lives only in React state for the session — never
  written to `localStorage`/`sessionStorage`/cookies. Closing the tab or
  hitting "Terminate Session" discards it immediately.
- The browser only ever talks to the console's own origin. nginx proxies
  `/api/*` to the gateway over the internal Docker network, so no CORS
  configuration is needed client-side and the gateway is never directly
  reachable from outside the network.
- `Content-Security-Policy` set in `agent/nginx.conf` restricts script
  sources to `'self'` (no inline scripts execute) and connections to
  `'self'` (no exfiltration to third-party origins via `fetch`).
- The nginx container runs as a non-root user with a read-only root
  filesystem (`tmpfs` only for `/var/cache/nginx`, `/var/run`, `/tmp`),
  `cap_drop: ALL`, and `no-new-privileges`.

## Local development (without Docker)

```bash
# terminal 1 — gateway, against a local Postgres + Ollama
cd gateway && uvicorn app.gateway:app --reload --port 8080

# terminal 2 — console, with hot reload
cd agent && npm install && npm run dev
```

`agent/vite.config.ts` proxies `/api` to `http://localhost:8080` in dev
mode, so the console code is identical between dev and the Dockerized
nginx setup.
