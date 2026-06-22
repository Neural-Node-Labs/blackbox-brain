# Ollama Security Gateway — Docker Deployment

## Layout
```
.
├── app/
│   ├── __init__.py
│   └── gateway.py        # FastAPI app
├── Dockerfile             # multi-stage, non-root runtime
├── docker-compose.yml     # gateway + postgres
├── schema.sql              # DDL (also auto-applied by SQLAlchemy at boot)
├── requirements.txt        # pinned deps
├── .env.example
└── .dockerignore
```

## Quick start

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, ALLOWED_ORIGINS, SEED_ADMIN_PASSWORD

docker compose up -d --build
docker compose logs -f gateway
curl -s http://localhost:8080/healthz
```

The app **refuses to start** in production if `SEED_ADMIN_PASSWORD` is unset
and no admin user yet exists — this is intentional, see "Fixes from review"
below.

> This environment has no Docker daemon available, so the build itself
> could not be executed here. The Dockerfile, compose file, and YAML have
> been syntax-validated; please run `docker compose build` in your own
> environment as the final gate before merging.

## Fixes applied in this validation pass

1. **Removed plaintext-password-in-logs path.** The original auto-seed
   logic generated a random admin password and logged it in cleartext when
   `SEED_ADMIN_PASSWORD` wasn't set. Logs are routinely shipped to
   aggregators with broader read access than the DB itself, so this was a
   credential-leak vector. The gateway now fails fast at startup in
   production if no seed password is provided, forcing an explicit
   operator decision instead of a silently logged secret.
2. **Rate limiter memory growth.** The in-memory sliding-window limiter
   had no cleanup path — IPs and token hashes from one-off users would
   accumulate indefinitely. Added a `prune()` method and a background
   janitor task (runs every 5 min, cancelled cleanly on shutdown) plus a
   hard cap as a backstop.
3. **Chat endpoint rate-limited before DB hit.** Token-hash rate limiting
   now happens before the database lookup, so a brute-force token-guessing
   client gets throttled without generating a query per attempt.
4. **Tightened input validation.** `ChatMessage.role` is now constrained
   to an enum-like pattern, `content` has a max length, `model` is
   pattern-restricted to safe characters, and message-array length is
   capped — reduces both abuse surface and accidental huge-payload DoS.
5. **Request body size cap.** Added middleware rejecting requests over
   1 MiB by default (`MAX_REQUEST_BODY_BYTES`), before they reach Pydantic
   parsing.
6. **Malformed-token short-circuit.** Bearer tokens not matching the
   expected 64-hex-char shape are rejected with 401 before touching the
   DB or rate limiter, cutting noise from garbage input.
7. **Global exception handler added.** Any unhandled exception now returns
   a generic 500 instead of letting a stack trace or internal detail leak
   to the client; the original error is still logged server-side.
8. **CORS origin validation.** `ALLOWED_ORIGINS` is validated at startup
   to explicitly reject a wildcard `*`, since the gateway also sets
   `allow_credentials=True` (combining the two is a known CORS
   misconfiguration that defeats the origin restriction).
9. **`docs_url`/`redoc_url` disabled.** No need to expose interactive
   OpenAPI UI on an internet-facing auth-gated gateway.
10. **Dependency pin fix.** `bcrypt` pinned to `4.0.1` (matches what
    `passlib==1.7.4` actually probes for); the unpinned later bcrypt
    version triggers a passlib version-detection warning on every
    process start, which is the kind of noisy, ignorable-looking warning
    that hides real issues in production logs over time.
11. **Container hardening:** non-root user (uid 1000), multi-stage build
    (no compilers/dev headers in the runtime image), `read_only: true`
    root filesystem with a `tmpfs` for `/tmp`, `cap_drop: ALL`,
    `no-new-privileges`, Postgres has no published host port (reachable
    only from the gateway over the internal bridge network), and a
    container `HEALTHCHECK`.
12. **DB session cleanup.** `get_db()` now explicitly closes the session
    in a `finally` block rather than relying solely on the `async with`
    exit timing relative to dependency teardown order.

## Static analysis run as part of this pass

```bash
pip install pyflakes bandit
pyflakes app/gateway.py      # → no output (clean)
bandit -r app/gateway.py     # → "No issues identified." (0 High/Med/Low)
```

Both were re-run after each fix above; final state is clean on both tools.
Also confirmed: `python -m py_compile app/gateway.py` succeeds, and the
module imports cleanly under the pinned dependency set with no warnings.

## Project workspace API

All endpoints below require `Authorization: Bearer <token>` and are rate
limited independently from `/api/chat` (30 req/min/token via
`workspace_limiter`). Project names: `^[A-Za-z0-9_-]{1,64}$`. File paths
are relative, validated against directory traversal, and resolved inside
`WORKSPACE_ROOT/<project>/` before any filesystem call.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/projects` | Create a project — body `{"name": "myproj"}` |
| `GET` | `/api/projects` | List projects with file count + total bytes |
| `DELETE` | `/api/projects/{name}` | Delete a project and all its files |
| `GET` | `/api/projects/{name}/files` | List files in a project (recursive) |
| `POST` | `/api/projects/{name}/files` | Upload a file — multipart form, fields `file` and optional `path` (defaults to the uploaded filename) |
| `DELETE` | `/api/projects/{name}/files/{path}` | Delete a single file |
| `GET` | `/api/projects/{name}/download` | Download the whole project as a `.zip` |

Limits: `MAX_UPLOAD_BYTES` (default 20 MB) per file, `MAX_PROJECT_BYTES`
(default 200 MB) per project — both configurable via env. Storage lives
at `WORKSPACE_ROOT` (default `/data/workspaces`), backed by a named
Docker volume so it survives container restarts.

```bash
TOKEN="<paste token from login>"
BASE=http://localhost:8080

curl -s -X POST $BASE/api/projects -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"demo"}'

curl -s $BASE/api/projects -H "Authorization: Bearer $TOKEN"

curl -s -X POST $BASE/api/projects/demo/files \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@./main.py" -F "path=src/main.py"

curl -s $BASE/api/projects/demo/files -H "Authorization: Bearer $TOKEN"

curl -s -OJ $BASE/api/projects/demo/download -H "Authorization: Bearer $TOKEN"

curl -s -X DELETE $BASE/api/projects/demo/files/src/main.py \
  -H "Authorization: Bearer $TOKEN"

curl -s -X DELETE $BASE/api/projects/demo -H "Authorization: Bearer $TOKEN"
```

## Known limitations to flag explicitly to reviewers

- **In-memory rate limiter is single-process.** Fine for one gateway
  replica. If you horizontally scale (multiple containers/workers behind
  a load balancer), limits won't be shared across processes — swap in a
  Redis-backed limiter (`INCR` + `EXPIRE`, or a sorted-set sliding window)
  before scaling out.
- **`--proxy-headers` is enabled in the Dockerfile CMD but
  `--forwarded-allow-ips` is left at uvicorn's default (loopback only).**
  If you terminate TLS at an external reverse proxy and need
  `X-Forwarded-For` honored for rate-limiting by real client IP, you must
  explicitly trust that proxy's IP — do not set this to `*`.
- **No token refresh/rotation endpoint.** Tokens are flat 60-minute
  bearer tokens; there's no `/refresh` flow. Acceptable for short-lived
  internal tool use; add rotation if this becomes a longer-lived
  user-facing product.
- **No structured audit log / SIEM export** for auth failures beyond
  standard logging — add if compliance requires it.

## curl smoke tests

```bash
BASE=http://localhost:8080

# 1. Login
curl -s -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<SEED_ADMIN_PASSWORD value>"}'

# 2. Authenticated streaming chat
TOKEN="<paste token from step 1>"
curl -N -X POST $BASE/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3","messages":[{"role":"user","content":"hello"}],"stream":true}'

# 3. Login rate limit (6th attempt in 60s -> 429)
for i in $(seq 1 6); do
  curl -s -o /dev/null -w "Attempt $i -> %{http_code}\n" \
    -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"wrongpassword"}'
done

# 4. Malformed/invalid token -> 401/403
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/api/chat \
  -H "Authorization: Bearer not-a-real-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3","messages":[{"role":"user","content":"hi"}]}'

# 5. Oversized body -> 413
python3 -c "print('{\"model\":\"llama3\",\"messages\":[{\"role\":\"user\",\"content\":\"' + 'A'*2_000_000 + '\"}]}')" > /tmp/big.json
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/api/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @/tmp/big.json
```
