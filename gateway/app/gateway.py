"""
Production-hardened API Gateway for Ollama (/api/chat) proxying.

Run locally:
    uvicorn app.gateway:app --host 0.0.0.0 --port 8080

Run in Docker: see Dockerfile / docker-compose.yml in repo root.

Required env vars (see Settings below):
    DATABASE_URL, OLLAMA_BASE_URL, ALLOWED_ORIGINS, SEED_ADMIN_PASSWORD
"""

import asyncio
import hashlib
import io
import logging
import re
import secrets
import shutil
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx
import asyncpg
from fastapi import FastAPI, Request, HTTPException, Depends, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, select, delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
    AsyncAttrs,
)
from sqlalchemy.orm import DeclarativeBase

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gateway")

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", populate_by_name=True, extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    ollama_base_url: str = Field("http://localhost:11434", alias="OLLAMA_BASE_URL")
    allowed_origins: str = Field(..., alias="ALLOWED_ORIGINS")
    seed_admin_password: Optional[str] = Field(None, alias="SEED_ADMIN_PASSWORD")
    token_ttl_minutes: int = Field(60, alias="TOKEN_TTL_MINUTES")
    environment: str = Field("production", alias="ENVIRONMENT")
    max_request_body_bytes: int = Field(1_048_576, alias="MAX_REQUEST_BODY_BYTES")  # 1 MiB
    max_upload_bytes: int = Field(20_000_000, alias="MAX_UPLOAD_BYTES")  # 20 MB per file
    max_project_bytes: int = Field(200_000_000, alias="MAX_PROJECT_BYTES")  # 200 MB per project
    workspace_root: str = Field("/data/workspaces", alias="WORKSPACE_ROOT")

    @field_validator("allowed_origins")
    @classmethod
    def no_wildcard_origins(cls, v: str) -> str:
        if "*" in v:
            raise ValueError("ALLOWED_ORIGINS must not contain a wildcard '*' origin")
        return v


settings = Settings()
CORS_ORIGINS = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
if not CORS_ORIGINS:
    raise RuntimeError("ALLOWED_ORIGINS must contain at least one valid origin")

WORKSPACE_ROOT = Path(settings.workspace_root).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# --------------------------------------------------------------------------
# Database setup
# --------------------------------------------------------------------------
class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)


class ActiveToken(Base):
    __tablename__ = "active_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False, index=True)


engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# --------------------------------------------------------------------------
# Security primitives
# --------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Precomputed dummy hash used to equalize timing when a username does not
# exist, mitigating user-enumeration via response-time side channel.
_DUMMY_HASH = pwd_context.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_token() -> str:
    """64 hex characters (32 random bytes), CSPRNG-backed."""
    return secrets.token_hex(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Sliding-window rate limiter (in-memory, asyncio-safe, self-pruning)
#
# NOTE: This limiter holds state in process memory. It is correct for a
# single-process deployment. If you scale to multiple uvicorn workers or
# multiple replicas, back this with Redis (e.g. INCR + EXPIRE / sorted sets)
# so limits are enforced consistently across processes.
# --------------------------------------------------------------------------
class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int, max_tracked_keys: int = 50_000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_tracked_keys = max_tracked_keys
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            q = self._hits[key]
            cutoff = now - self.window_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True

    async def prune(self) -> None:
        """Drop empty/stale entries to bound memory growth over time."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            stale_keys = []
            for k, q in self._hits.items():
                while q and q[0] < cutoff:
                    q.popleft()
                if not q:
                    stale_keys.append(k)
            for k in stale_keys:
                del self._hits[k]
            # Hard cap as a defensive backstop against pathological growth
            if len(self._hits) > self.max_tracked_keys:
                overflow = len(self._hits) - self.max_tracked_keys
                for k in list(self._hits.keys())[:overflow]:
                    del self._hits[k]


login_limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60)
chat_limiter = SlidingWindowRateLimiter(max_requests=60, window_seconds=60)


async def _rate_limiter_janitor():
    """Background task: periodically prunes rate-limiter state."""
    while True:
        await asyncio.sleep(300)
        try:
            await login_limiter.prune()
            await chat_limiter.prune()
        except Exception:  # never let the janitor crash the app
            logger.exception("Rate limiter janitor encountered an error")


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)


class LoginResponse(BaseModel):
    token: str
    expires_at: str


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str = Field(..., min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:\-]+$")
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    stream: bool = True


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def valid_project_name(cls, v: str) -> str:
        if not PROJECT_NAME_RE.match(v):
            raise ValueError(
                "Project name must be 1-64 characters: letters, digits, underscore, hyphen only"
            )
        if v in (".", ".."):
            raise ValueError("Invalid project name")
        return v


class ProjectInfo(BaseModel):
    name: str
    file_count: int
    total_bytes: int


class FileInfo(BaseModel):
    path: str
    size_bytes: int
    modified_at: str


# --------------------------------------------------------------------------
# Workspace filesystem helpers
#
# All paths are validated against directory-traversal before any
# filesystem call. Project names and file paths come directly from
# clients, so every join is treated as untrusted input.
# --------------------------------------------------------------------------
def project_dir(project_name: str) -> Path:
    if not PROJECT_NAME_RE.match(project_name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project name")
    candidate = (WORKSPACE_ROOT / project_name).resolve()
    if candidate.parent != WORKSPACE_ROOT:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project name")
    return candidate


def safe_file_path(project_name: str, relative_path: str) -> Path:
    """Resolve a client-supplied relative path inside a project, rejecting
    any attempt to escape the project directory (.., absolute paths,
    symlink tricks, null bytes, etc.)."""
    base = project_dir(project_name)
    if not base.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if "\x00" in relative_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path")

    pure = PurePosixPath(relative_path.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path")

    candidate = (base / pure).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file path")
    return candidate


def _list_project_files_sync(base: Path) -> list[FileInfo]:
    results: list[FileInfo] = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            stat = p.stat()
            results.append(
                FileInfo(
                    path=str(p.relative_to(base).as_posix()),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                )
            )
    return results


def _project_usage_sync(base: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for p in base.rglob("*"):
        if p.is_file():
            count += 1
            total += p.stat().st_size
    return count, total


def _write_upload_sync(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _build_zip_sync(base: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(base).as_posix()))
    return buf.getvalue()


workspace_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)


# --------------------------------------------------------------------------
# Lifespan: httpx client pool + DB seeding + background janitor
# --------------------------------------------------------------------------
def _find_auth_error(exc: BaseException) -> Optional[asyncpg.exceptions.InvalidAuthorizationSpecificationError]:
    """Walks the exception cause/context chain looking for a Postgres
    authentication failure, which SQLAlchemy/asyncpg may wrap inside an
    OperationalError rather than raise directly."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, asyncpg.exceptions.InvalidAuthorizationSpecificationError):
            return current
        current = current.__cause__ or current.__context__
    return None


async def wait_for_database(max_attempts: int = 10, base_delay: float = 1.0) -> None:
    """
    Retries the initial database connection with exponential backoff.

    On first boot, the gateway and Postgres containers can both report
    "started" within the same second, and on some Docker network drivers
    (notably Docker Desktop's) the embedded DNS entry for a just-created
    service can lag a moment behind the container actually accepting
    connections. Rather than crash the whole app on that race, retry a
    bounded number of times before giving up for real (e.g. genuinely
    wrong DATABASE_URL, Postgres down, network misconfigured).

    Authentication failures are NOT retried: a wrong password will still
    be wrong on attempt 10, and retrying just delays a clear error by
    several minutes. This is also the classic symptom of POSTGRES_PASSWORD
    having changed in .env after the Postgres data volume was already
    initialized with the old password — Postgres only applies
    POSTGRES_PASSWORD on first volume init, so the two drift apart.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.connect():
                pass
            if attempt > 1:
                logger.info("Database connection succeeded on attempt %d/%d.", attempt, max_attempts)
            return
        except Exception as exc:  # noqa: BLE001 - intentionally broad: any connect failure should retry
            auth_error = _find_auth_error(exc)
            if auth_error is not None:
                raise RuntimeError(
                    "Database authentication failed — this will not resolve by retrying. "
                    "The most common cause: POSTGRES_PASSWORD in .env doesn't match the "
                    "password baked into the existing 'pgdata' volume (Postgres only applies "
                    "POSTGRES_PASSWORD the first time it initializes an empty volume). Fix by "
                    "either resetting the password inside Postgres to match .env "
                    "(docker compose exec postgres psql -U <user> -c \"ALTER USER <user> "
                    "WITH PASSWORD '<password>';\"), or by wiping the volume with "
                    "'docker compose down -v' if the data is disposable."
                ) from auth_error

            last_error = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Database not reachable yet (attempt %d/%d): %s. Retrying in %.1fs…",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"Could not connect to the database after {max_attempts} attempts. "
        f"Last error: {last_error}. Check DATABASE_URL, that the postgres "
        f"service is healthy, and that both containers share the same "
        f"Docker network."
    ) from last_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    await wait_for_database()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await seed_admin_if_missing()

    janitor_task = asyncio.create_task(_rate_limiter_janitor())

    logger.info("Gateway startup complete. Environment=%s", settings.environment)
    yield

    janitor_task.cancel()
    try:
        await janitor_task
    except asyncio.CancelledError:
        pass

    await app.state.http_client.aclose()
    await engine.dispose()
    logger.info("Gateway shutdown complete.")


async def seed_admin_if_missing():
    """
    Seed a default 'admin' user on first boot.

    SECURITY: the seed password must be supplied explicitly via
    SEED_ADMIN_PASSWORD. We deliberately do NOT auto-generate and log a
    plaintext password, since application logs are frequently aggregated
    to third-party systems and would otherwise leak a working credential.
    In production this function fails fast if the env var is absent and
    no admin user yet exists, forcing an explicit operator decision.
    """
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.username == "admin"))
        existing = result.scalar_one_or_none()
        if existing is not None:
            logger.info("Admin seed user already present; skipping seed.")
            return

        if not settings.seed_admin_password:
            if settings.environment == "production":
                raise RuntimeError(
                    "No 'admin' user exists and SEED_ADMIN_PASSWORD is not set. "
                    "Refusing to start in production without an explicit seed password."
                )
            # Non-production convenience path only — never reachable in prod.
            logger.warning(
                "ENVIRONMENT=%s and SEED_ADMIN_PASSWORD unset; skipping admin seed. "
                "Create a user manually before testing auth.",
                settings.environment,
            )
            return

        if len(settings.seed_admin_password) < 12:
            raise RuntimeError("SEED_ADMIN_PASSWORD must be at least 12 characters long.")

        user = User(username="admin", password_hash=hash_password(settings.seed_admin_password))
        session.add(user)
        await session.commit()
        logger.info("Seeded default 'admin' user from SEED_ADMIN_PASSWORD.")


# --------------------------------------------------------------------------
# App + middleware
# --------------------------------------------------------------------------
app = FastAPI(title="Ollama Security Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        is_upload = request.method == "POST" and request.url.path.endswith("/files")
        limit = settings.max_upload_bytes if is_upload else settings.max_request_body_bytes
        try:
            if int(content_length) > limit:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body too large"},
                )
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid Content-Length header"},
            )
    return await call_next(request)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# Auth dependency: validates Bearer token against active_tokens
# --------------------------------------------------------------------------
async def _validate_bearer_token(
    request: Request, db: AsyncSession, limiter: "SlidingWindowRateLimiter"
) -> ActiveToken:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    plaintext_token = auth_header[len("Bearer "):].strip()
    if not plaintext_token or len(plaintext_token) != 64:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed bearer token")

    token_hash = hash_token(plaintext_token)

    # Rate-limit by token hash BEFORE querying, so brute-force token
    # guessing is throttled cheaply without hitting the database at all.
    if not await limiter.allow(token_hash):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    result = await db.execute(select(ActiveToken).where(ActiveToken.token_hash == token_hash))
    token_row = result.scalar_one_or_none()

    if token_row is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")

    now = datetime.now(timezone.utc)
    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < now:
        await db.execute(delete(ActiveToken).where(ActiveToken.id == token_row.id))
        await db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token expired")

    return token_row


async def require_valid_token(request: Request, db: AsyncSession = Depends(get_db)) -> ActiveToken:
    """Auth dependency for /api/chat — rate-limited against chat_limiter
    (60 req/min/token)."""
    return await _validate_bearer_token(request, db, chat_limiter)


async def require_valid_token_ws(request: Request, db: AsyncSession = Depends(get_db)) -> ActiveToken:
    """Auth dependency for workspace endpoints — separate, independent
    rate limit (30 req/min/token) so file operations don't compete with
    chat-streaming quota or vice versa."""
    return await _validate_bearer_token(request, db, workspace_limiter)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/api/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    if not await login_limiter.allow(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()

    # Always run a bcrypt verify, even for unknown usernames, using a fixed
    # dummy hash, so response timing does not reveal whether the username
    # exists.
    valid = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)

    if not user or not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    plaintext_token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.token_ttl_minutes)

    new_token = ActiveToken(
        user_id=user.id,
        token_hash=hash_token(plaintext_token),
        expires_at=expires_at,
    )
    db.add(new_token)
    await db.commit()

    return LoginResponse(token=plaintext_token, expires_at=expires_at.isoformat())


@app.post("/api/chat")
async def chat_proxy(
    payload: ChatRequest,
    request: Request,
    _token_row: ActiveToken = Depends(require_valid_token),
):
    client: httpx.AsyncClient = request.app.state.http_client

    async def stream_from_ollama():
        try:
            async with client.stream(
                "POST",
                "/api/chat",
                json=payload.model_dump(),
            ) as upstream:
                if upstream.status_code >= 400:
                    body = await upstream.aread()
                    logger.error(
                        "Ollama returned status %s: %s",
                        upstream.status_code,
                        body[:500],
                    )
                    yield b'{"error": "upstream model service error"}'
                    return
                async for chunk in upstream.aiter_bytes():
                    yield chunk
        except httpx.RequestError as exc:
            logger.error("Error contacting Ollama upstream: %s", exc)
            yield b'{"error": "upstream service unavailable"}'
        except Exception:
            # Catch-all so an unexpected error never leaks a stack trace or
            # internal detail to the client mid-stream.
            logger.exception("Unexpected error while streaming from Ollama")
            yield b'{"error": "internal gateway error"}'

    return StreamingResponse(stream_from_ollama(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------
# Workspace routes: projects + files
#
# Layout on disk: WORKSPACE_ROOT/<project_name>/<relative file path>
# Every project/file path supplied by the client is validated by
# project_dir()/safe_file_path() before touching the filesystem. All
# blocking filesystem work runs via asyncio.to_thread so the event loop
# is never blocked by disk I/O.
# --------------------------------------------------------------------------
@app.post("/api/projects", response_model=ProjectInfo, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    base = project_dir(payload.name)
    if base.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project already exists")

    await asyncio.to_thread(base.mkdir, parents=True)
    return ProjectInfo(name=payload.name, file_count=0, total_bytes=0)


@app.get("/api/projects", response_model=list[ProjectInfo])
async def list_projects(_token_row: ActiveToken = Depends(require_valid_token_ws)):
    def _scan() -> list[ProjectInfo]:
        infos: list[ProjectInfo] = []
        if not WORKSPACE_ROOT.exists():
            return infos
        for entry in sorted(WORKSPACE_ROOT.iterdir()):
            if entry.is_dir() and PROJECT_NAME_RE.match(entry.name):
                count, total = _project_usage_sync(entry)
                infos.append(ProjectInfo(name=entry.name, file_count=count, total_bytes=total))
        return infos

    return await asyncio.to_thread(_scan)


@app.delete("/api/projects/{project_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_name: str,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    base = project_dir(project_name)
    if not base.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    await asyncio.to_thread(shutil.rmtree, base)
    return None


@app.get("/api/projects/{project_name}/files", response_model=list[FileInfo])
async def list_project_files(
    project_name: str,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    base = project_dir(project_name)
    if not base.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return await asyncio.to_thread(_list_project_files_sync, base)


@app.post("/api/projects/{project_name}/files", response_model=FileInfo, status_code=status.HTTP_201_CREATED)
async def upload_project_file(
    project_name: str,
    file: UploadFile = File(...),
    path: Optional[str] = Form(None),
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    base = project_dir(project_name)
    if not base.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    relative_path = path or file.filename
    if not relative_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing destination file name")

    dest = safe_file_path(project_name, relative_path)

    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_bytes} byte limit",
        )

    _, current_total = await asyncio.to_thread(_project_usage_sync, base)
    if current_total + len(data) > settings.max_project_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Project would exceed {settings.max_project_bytes} byte quota",
        )

    await asyncio.to_thread(_write_upload_sync, dest, data)

    stat = await asyncio.to_thread(dest.stat)
    return FileInfo(
        path=str(dest.relative_to(base).as_posix()),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


@app.delete("/api/projects/{project_name}/files/{file_path:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_file(
    project_name: str,
    file_path: str,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    target = safe_file_path(project_name, file_path)
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    await asyncio.to_thread(target.unlink)
    return None


@app.get("/api/projects/{project_name}/download")
async def download_project(
    project_name: str,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
):
    base = project_dir(project_name)
    if not base.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    zip_bytes = await asyncio.to_thread(_build_zip_sync, base)

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_name}.zip"'},
    )


@app.exception_handler(httpx.RequestError)
async def httpx_error_handler(request: Request, exc: httpx.RequestError):
    logger.error("Unhandled upstream error: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": "Bad gateway: upstream service error"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

