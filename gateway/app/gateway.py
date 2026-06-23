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
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import asyncssh
import httpx
import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, HTTPException, Depends, status, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, Boolean, Text, select, delete
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
    # SSH remote execution
    ssh_targets_file: Optional[str] = Field(None, alias="SSH_TARGETS_FILE")
    ssh_known_hosts_file: Optional[str] = Field(None, alias="SSH_KNOWN_HOSTS_FILE")
    ssh_command_timeout: int = Field(60, alias="SSH_COMMAND_TIMEOUT")
    ssh_max_output_bytes: int = Field(51_200, alias="SSH_MAX_OUTPUT_BYTES")  # 50 KB per stream

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


class SshJob(Base):
    __tablename__ = "ssh_jobs"
    id = Column(Integer, primary_key=True)
    job_id = Column(String(36), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_ids = Column(String(2048), nullable=False)   # JSON list of target ids
    procedure = Column(String(65535), nullable=False)
    status = Column(String(20), nullable=False, default="PENDING", index=True)
    results = Column(String(131072), nullable=True)     # JSON: {target_id: {stdout,stderr,exit_code,error,duration_ms}}
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)


class Procedure(Base):
    """Stored Osiris procedure definition."""
    __tablename__ = "procedures"
    id = Column(Integer, primary_key=True)
    procedure_id = Column(String(36), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(1000), nullable=True)
    body = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False)


class ProcedureRun(Base):
    """One execution of a Procedure across one or more SSH targets."""
    __tablename__ = "procedure_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(36), unique=True, nullable=False, index=True)
    procedure_id = Column(String(36), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_ids = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING", index=True)
    procedure_snapshot = Column(Text, nullable=False)
    results = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    triggered_by = Column(String(50), nullable=False, default="manual")


class RunStepLog(Base):
    """Per-step telemetry within a ProcedureRun on one target."""
    __tablename__ = "run_step_logs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(36), nullable=False, index=True)
    target_id = Column(String(200), nullable=False, index=True)
    step_id = Column(String(200), nullable=False)
    step_name = Column(String(500), nullable=True)
    action = Column(String(100), nullable=True)
    command = Column(Text, nullable=True)
    reasoning = Column(Text, nullable=True)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    exit_code = Column(Integer, nullable=True)
    status = Column(String(30), nullable=False, default="PENDING")
    attempt = Column(Integer, nullable=False, default=1)
    fix_applied = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    logged_at = Column(TIMESTAMP(timezone=True), nullable=False)


class Schedule(Base):
    """Cron-style schedule for automatic procedure execution."""
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    schedule_id = Column(String(36), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    procedure_id = Column(String(36), nullable=False, index=True)
    target_ids = Column(Text, nullable=False)
    cron_expr = Column(String(100), nullable=False)
    label = Column(String(200), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(TIMESTAMP(timezone=True), nullable=True)
    next_run_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)


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
            await workspace_limiter.prune()
            await ssh_exec_limiter.prune()
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


# --------------------------------------------------------------------------
# SSH remote-execution: target config, schemas, and execution engine
# --------------------------------------------------------------------------

@dataclass
class SshTarget:
    id: str
    label: str
    host: str
    user: str
    port: int = 22
    auth: str = "password"          # "password" | "key"
    password_env: Optional[str] = None   # name of env var holding the password
    key_env: Optional[str] = None        # name of env var holding PEM private key
    strict_host_checking: bool = False   # True → verify against known_hosts file
    tags: list = dc_field(default_factory=list)


def load_ssh_targets() -> list[SshTarget]:
    """Load targets from the operator-configured JSON file (SSH_TARGETS_FILE).
    Returns an empty list if the file is absent or invalid — errors are
    logged rather than crashing the app so unrelated features keep working."""
    targets_file = settings.ssh_targets_file
    if not targets_file:
        return []
    path = Path(targets_file)
    if not path.is_file():
        logger.warning("SSH_TARGETS_FILE %s does not exist; no targets loaded.", targets_file)
        return []
    try:
        raw: list[dict] = json.loads(path.read_text())
        out: list[SshTarget] = []
        for item in raw:
            try:
                out.append(SshTarget(**{k: v for k, v in item.items() if k in SshTarget.__dataclass_fields__}))
            except TypeError as exc:
                logger.warning("Skipping malformed SSH target %s: %s", item.get("id", "?"), exc)
        return out
    except Exception as exc:
        logger.error("Failed to load SSH targets from %s: %s", targets_file, exc)
        return []


# Pydantic schemas for SSH endpoints ----------------------------------------

class SshTargetOut(BaseModel):
    id: str
    label: str
    host: str
    user: str
    port: int
    auth: str
    tags: list[str]


class SshExecuteRequest(BaseModel):
    target_ids: list[str] = Field(..., min_length=1, max_length=50)
    procedure: str = Field(..., min_length=1, max_length=65_535)
    timeout: int = Field(default=60, ge=5, le=600)
    label: Optional[str] = Field(None, max_length=200)

    @field_validator("target_ids")
    @classmethod
    def no_empty_ids(cls, v: list[str]) -> list[str]:
        if any(not t.strip() for t in v):
            raise ValueError("target_ids must not contain empty strings")
        return v


class SshJobOut(BaseModel):
    job_id: str
    status: str
    target_ids: list[str]
    procedure: str
    label: Optional[str]
    results: Optional[dict]
    created_at: str
    completed_at: Optional[str]


# SSH execution engine --------------------------------------------------------

async def _run_on_target(
    target: SshTarget,
    procedure: str,
    timeout: int,
) -> dict:
    """Connect to a single SSH target and execute the procedure. Returns a
    result dict; never raises — all errors are captured and returned."""
    t0 = time.monotonic()

    # Resolve credentials from environment only — never from user input.
    password: Optional[str] = None
    client_keys: list = []

    if target.auth == "password":
        if not target.password_env:
            return {"error": "password_env not set for this target", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}
        password = os.environ.get(target.password_env)
        if not password:
            return {"error": f"Env var {target.password_env!r} is not set on the gateway", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}
    elif target.auth == "key":
        if not target.key_env:
            return {"error": "key_env not set for this target", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}
        pem = os.environ.get(target.key_env)
        if not pem:
            return {"error": f"Env var {target.key_env!r} is not set on the gateway", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}
        try:
            client_keys = [asyncssh.import_private_key(pem)]
        except Exception as exc:
            return {"error": f"Invalid private key in {target.key_env!r}: {exc}", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}

    known_hosts: Optional[str]
    if target.strict_host_checking:
        kh_file = settings.ssh_known_hosts_file or str(WORKSPACE_ROOT / ".ssh" / "known_hosts")
        known_hosts = kh_file if Path(kh_file).is_file() else None
    else:
        known_hosts = None  # asyncssh: None → skip host-key verification

    limit = settings.ssh_max_output_bytes
    try:
        async with asyncssh.connect(
            host=target.host,
            port=target.port,
            username=target.user,
            password=password,
            client_keys=client_keys,
            known_hosts=known_hosts,
            connect_timeout=10,
        ) as conn:
            # Multi-line procedures are piped to bash -s; single-line run directly.
            is_script = "\n" in procedure.strip()
            if is_script:
                result = await asyncio.wait_for(
                    conn.run("bash -s", input=procedure, check=False),
                    timeout=float(timeout),
                )
            else:
                result = await asyncio.wait_for(
                    conn.run(procedure, check=False),
                    timeout=float(timeout),
                )
            return {
                "stdout": (result.stdout or "")[:limit],
                "stderr": (result.stderr or "")[:limit // 4],
                "exit_code": result.exit_status if result.exit_status is not None else -1,
                "error": None,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
    except asyncssh.PermissionDenied:
        return {"error": "SSH authentication failed — check credentials in env vars", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": int((time.monotonic() - t0) * 1000)}
    except asyncssh.HostKeyNotVerifiable as exc:
        return {"error": f"Host key not trusted. Enable strict_host_checking=false or add key to known_hosts. ({exc})", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 0}
    except asyncio.TimeoutError:
        return {"error": f"Procedure timed out after {timeout}s", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": timeout * 1000}
    except (asyncssh.ConnectionLost, asyncssh.DisconnectError) as exc:
        return {"error": f"SSH connection lost: {exc}", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": int((time.monotonic() - t0) * 1000)}
    except OSError as exc:
        return {"error": f"Network error connecting to {target.host}: {exc}", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        logger.error("Unexpected SSH error on target %s: %s", target.id, exc)
        return {"error": "Unexpected SSH error — see gateway logs", "exit_code": -1, "stdout": "", "stderr": "", "duration_ms": int((time.monotonic() - t0) * 1000)}


async def _execute_ssh_job(job_id: str, targets: list[SshTarget], procedure: str, timeout: int) -> None:
    """Background task: run the procedure on all targets concurrently, update DB."""
    async with SessionLocal() as db:
        # Mark RUNNING
        result = await db.execute(select(SshJob).where(SshJob.job_id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return
        job.status = "RUNNING"
        await db.commit()

    # Run all targets concurrently
    tasks = {t.id: _run_on_target(t, procedure, timeout) for t in targets}
    results: dict[str, dict] = {}
    async with asyncio.TaskGroup() as tg:
        async def _run_and_collect(target_id: str, coro):
            results[target_id] = await coro
        for tid, coro in tasks.items():
            tg.create_task(_run_and_collect(tid, coro))

    # Determine overall status
    all_ok = all(r.get("exit_code", -1) == 0 and not r.get("error") for r in results.values())
    any_ok = any(r.get("exit_code", -1) == 0 and not r.get("error") for r in results.values())
    if all_ok:
        final_status = "SUCCESS"
    elif any_ok:
        final_status = "PARTIAL"
    else:
        final_status = "FAILED"

    async with SessionLocal() as db:
        result = await db.execute(select(SshJob).where(SshJob.job_id == job_id))
        job = result.scalar_one_or_none()
        if job:
            job.status = final_status
            job.results = json.dumps(results)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
    logger.info("SSH job %s completed: %s (%d targets)", job_id, final_status, len(results))


def _ssh_job_to_out(job: SshJob) -> SshJobOut:
    return SshJobOut(
        job_id=job.job_id,
        status=job.status,
        target_ids=json.loads(job.target_ids),
        procedure=job.procedure,
        label=None,
        results=json.loads(job.results) if job.results else None,
        created_at=job.created_at.isoformat() if job.created_at else "",
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


async def require_valid_token_ssh(request: Request, db: AsyncSession = Depends(get_db)) -> ActiveToken:
    """Auth dependency for SSH endpoints — tighter rate limit than workspace."""
    return await _validate_bearer_token(request, db, ssh_exec_limiter)


class ChatRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:\-]+$")
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    stream: bool = True
    options: Optional[dict] = None  # passed through to Ollama as-is (temperature, num_predict, etc.)


# --------------------------------------------------------------------------
# Osiris Procedure schemas
# --------------------------------------------------------------------------

class OsirisValidationSpec(BaseModel):
    type: str = Field(..., pattern="^(exit_code|file_contains|file_exists|regex_match|http_status|kubectl_status|any)$")
    expected: Optional[str] = None
    path: Optional[str] = None


class OsirisStep(BaseModel):
    step_id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=300)
    action: str = Field(..., pattern="^(shell|docker|kubernetes|terraform|http|ssh)$")
    command: str = Field(..., min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    validation: Optional[OsirisValidationSpec] = None
    on_failure: str = Field(default="halt", pattern="^(auto_fix|halt|skip)$")
    max_retries: int = Field(default=3, ge=0, le=10)
    reasoning: Optional[str] = None  # operator-supplied rationale shown in telemetry


class OsirisBody(BaseModel):
    procedure_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    steps: list[OsirisStep] = Field(..., min_length=1, max_length=100)

    @field_validator("steps")
    @classmethod
    def no_cycles(cls, steps: list[OsirisStep]) -> list[OsirisStep]:
        ids = {s.step_id for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep not in ids:
                    raise ValueError(f"Step '{s.step_id}' depends_on unknown step '{dep}'")
        # Kahn's algorithm cycle check
        in_degree: dict[str, int] = {s.step_id: 0 for s in steps}
        graph: dict[str, list[str]] = {s.step_id: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                graph[dep].append(s.step_id)
                in_degree[s.step_id] += 1
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for child in graph.get(node, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        if visited != len(steps):
            raise ValueError("Procedure steps contain a dependency cycle (DAG cycle detected)")
        return steps


class ProcedureCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    body: OsirisBody


class ProcedureUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    body: Optional[OsirisBody] = None


class ProcedureOut(BaseModel):
    procedure_id: str
    name: str
    description: Optional[str]
    version: int
    step_count: int
    created_at: str
    updated_at: str


class RunRequest(BaseModel):
    procedure_id: str
    target_ids: list[str] = Field(..., min_length=1, max_length=50)
    timeout_per_step: int = Field(default=120, ge=10, le=3600)


class StepLogOut(BaseModel):
    id: int
    run_id: str
    target_id: str
    step_id: str
    step_name: Optional[str]
    action: Optional[str]
    command: Optional[str]
    reasoning: Optional[str]
    stdout: Optional[str]
    stderr: Optional[str]
    exit_code: Optional[int]
    status: str
    attempt: int
    fix_applied: Optional[str]
    duration_ms: Optional[int]
    logged_at: str


class RunOut(BaseModel):
    run_id: str
    procedure_id: Optional[str]
    status: str
    target_ids: list[str]
    triggered_by: str
    results: Optional[dict]
    created_at: str
    completed_at: Optional[str]


class ScheduleCreate(BaseModel):
    procedure_id: str
    target_ids: list[str] = Field(..., min_length=1, max_length=50)
    cron_expr: str = Field(..., min_length=9, max_length=100)
    label: Optional[str] = Field(None, max_length=200)
    enabled: bool = True

    @field_validator("cron_expr")
    @classmethod
    def valid_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError("cron_expr must have 5 space-separated fields: min hour dom month dow")
        return v.strip()


class ScheduleOut(BaseModel):
    schedule_id: str
    procedure_id: str
    target_ids: list[str]
    cron_expr: str
    label: Optional[str]
    enabled: bool
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    created_at: str


# --------------------------------------------------------------------------
# Osiris DAG execution engine
# --------------------------------------------------------------------------

def _compute_waves(steps: list[OsirisStep]) -> list[list[OsirisStep]]:
    """Topological sort → execution waves. All steps in a wave have their
    dependencies satisfied by prior waves."""
    remaining = {s.step_id: s for s in steps}
    completed: set[str] = set()
    waves: list[list[OsirisStep]] = []

    while remaining:
        ready = [s for s in remaining.values() if all(d in completed for d in s.depends_on)]
        if not ready:
            stuck = list(remaining.keys())
            raise RuntimeError(f"DAG deadlock — steps with unresolvable deps: {stuck}")
        for s in ready:
            del remaining[s.step_id]
        completed.update(s.step_id for s in ready)
        waves.append(ready)
    return waves


def _validate_step_result(step: OsirisStep, stdout: str, stderr: str, exit_code: int) -> tuple[bool, str]:
    """Deterministic validation — no LLM. Returns (passed, reason)."""
    spec = step.validation
    if spec is None:
        passed = exit_code == 0
        return passed, "exit_code == 0" if passed else f"exit_code {exit_code} != 0"

    vtype = spec.type
    if vtype == "any":
        return True, "any — always passes"
    if vtype == "exit_code":
        expected = int(spec.expected or "0")
        ok = exit_code == expected
        return ok, f"exit_code {exit_code} == {expected}" if ok else f"exit_code {exit_code} != {expected}"
    if vtype == "regex_match":
        import re as _re
        haystack = stdout + "\n" + stderr
        ok = bool(_re.search(spec.expected or "", haystack))
        return ok, ("regex matched" if ok else f"regex '{spec.expected}' not found in output")
    if vtype == "file_exists":
        p = Path(spec.path or "")
        ok = p.exists()
        return ok, (f"{spec.path} exists" if ok else f"{spec.path} not found")
    if vtype == "file_contains":
        p = Path(spec.path or "")
        if not p.exists():
            return False, f"{spec.path} not found"
        ok = (spec.expected or "") in p.read_text(errors="replace")
        return ok, ("file contains expected string" if ok else "file missing expected string")
    if vtype == "http_status":
        # http validation not feasible synchronously from here without
        # an async context; fall back to exit code check
        ok = exit_code == 0
        return ok, f"http_status validation fell back to exit_code: {exit_code}"
    return False, f"Unsupported validation type: {vtype}"


async def _run_step_on_target(
    run_id: str,
    step: OsirisStep,
    target: SshTarget,
    timeout: int,
) -> dict:
    """Execute one step on one target via SSH, with retry + deterministic validation."""
    for attempt in range(1, step.max_retries + 2):  # +1 for the initial attempt
        step_t0 = time.monotonic()
        ssh_result = await _run_on_target(target, step.command, timeout)
        duration = int((time.monotonic() - step_t0) * 1000)

        stdout = ssh_result.get("stdout", "")
        stderr = ssh_result.get("stderr", "")
        exit_code = ssh_result.get("exit_code", -1)
        ssh_error = ssh_result.get("error")

        passed, reason = _validate_step_result(step, stdout, stderr, exit_code)
        fix_applied = None

        if not passed and ssh_error:
            reason = ssh_error

        log_status = "COMPLETED" if passed else ("FAILED_TERMINAL" if attempt > step.max_retries else "FAILED_ATTEMPT")

        # Write step-level telemetry
        async with SessionLocal() as db:
            log = RunStepLog(
                run_id=run_id,
                target_id=target.id,
                step_id=step.step_id,
                step_name=step.name,
                action=step.action,
                command=step.command,
                reasoning=step.reasoning,
                stdout=stdout[:8192] if stdout else None,
                stderr=stderr[:2048] if stderr else None,
                exit_code=exit_code,
                status=log_status,
                attempt=attempt,
                fix_applied=fix_applied,
                duration_ms=duration,
                logged_at=datetime.now(timezone.utc),
            )
            db.add(log)
            await db.commit()

        if passed:
            return {"step_id": step.step_id, "status": "COMPLETED", "attempt": attempt,
                    "stdout": stdout, "stderr": stderr, "exit_code": exit_code, "duration_ms": duration}

        if step.on_failure == "skip":
            return {"step_id": step.step_id, "status": "SKIPPED", "reason": reason, "attempt": attempt}

        if step.on_failure == "halt" or attempt > step.max_retries:
            return {"step_id": step.step_id, "status": "FAILED_TERMINAL", "reason": reason,
                    "attempt": attempt, "stdout": stdout, "stderr": stderr, "exit_code": exit_code}

        logger.info("Step %s attempt %d failed (%s), retrying…", step.step_id, attempt, reason)
        await asyncio.sleep(min(2 ** (attempt - 1), 30))

    return {"step_id": step.step_id, "status": "FAILED_TERMINAL", "reason": "max retries exceeded", "attempt": step.max_retries + 1}


async def _run_procedure_on_target(run_id: str, body: OsirisBody, target: SshTarget, timeout: int) -> dict:
    """Execute all waves of a procedure on one target sequentially."""
    try:
        waves = _compute_waves(body.steps)
    except RuntimeError as exc:
        return {"target_id": target.id, "status": "FAILED", "error": str(exc), "steps": {}}

    step_results: dict[str, dict] = {}
    for wave_idx, wave in enumerate(waves):
        logger.info("Run %s target %s wave %d: %d steps", run_id, target.id, wave_idx, len(wave))
        # Steps within a wave run concurrently on this target
        tasks = [_run_step_on_target(run_id, step, target, timeout) for step in wave]
        wave_results = await asyncio.gather(*tasks, return_exceptions=True)
        for step, result in zip(wave, wave_results):
            if isinstance(result, Exception):
                result = {"step_id": step.step_id, "status": "FAILED_TERMINAL", "error": str(result)}
            step_results[step.step_id] = result
            if result.get("status") == "FAILED_TERMINAL" and step.on_failure == "halt":
                return {"target_id": target.id, "status": "FAILED", "halted_at": step.step_id, "steps": step_results}

    all_ok = all(r.get("status") in ("COMPLETED", "SKIPPED") for r in step_results.values())
    return {"target_id": target.id, "status": "SUCCESS" if all_ok else "PARTIAL", "steps": step_results}


async def _execute_procedure_run(run_id: str, body: OsirisBody, targets: list[SshTarget], timeout: int) -> None:
    """Background task: run procedure across all targets concurrently, update DB."""
    # Mark RUNNING
    async with SessionLocal() as db:
        result = await db.execute(select(ProcedureRun).where(ProcedureRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if run:
            run.status = "RUNNING"
            await db.commit()

    # All targets run concurrently
    coros = [_run_procedure_on_target(run_id, body, t, timeout) for t in targets]
    target_results = await asyncio.gather(*coros, return_exceptions=True)

    results: dict[str, dict] = {}
    for target, result in zip(targets, target_results):
        if isinstance(result, Exception):
            results[target.id] = {"status": "FAILED", "error": str(result), "steps": {}}
        else:
            results[target.id] = result

    all_ok = all(r.get("status") == "SUCCESS" for r in results.values())
    any_ok = any(r.get("status") in ("SUCCESS", "PARTIAL") for r in results.values())
    final_status = "SUCCESS" if all_ok else ("PARTIAL" if any_ok else "FAILED")

    async with SessionLocal() as db:
        result = await db.execute(select(ProcedureRun).where(ProcedureRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if run:
            run.status = final_status
            run.results = json.dumps(results)
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()

    logger.info("Procedure run %s completed: %s (%d targets)", run_id, final_status, len(targets))


async def _fire_schedule(schedule_id: str) -> None:
    """Called by APScheduler — load schedule + procedure, build target list, fire run."""
    async with SessionLocal() as db:
        sched_result = await db.execute(select(Schedule).where(Schedule.schedule_id == schedule_id))
        sched = sched_result.scalar_one_or_none()
        if not sched or not sched.enabled:
            return
        proc_result = await db.execute(select(Procedure).where(Procedure.procedure_id == sched.procedure_id))
        proc = proc_result.scalar_one_or_none()
        if not proc:
            logger.warning("Schedule %s references missing procedure %s", schedule_id, sched.procedure_id)
            return

        target_ids: list[str] = json.loads(sched.target_ids)
        all_targets = load_ssh_targets()
        targets = [t for t in all_targets if t.id in target_ids]
        if not targets:
            logger.warning("Schedule %s: no matching targets", schedule_id)
            return

        body = OsirisBody.model_validate(json.loads(proc.body))
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        new_run = ProcedureRun(
            run_id=run_id, procedure_id=proc.procedure_id, user_id=sched.user_id,
            target_ids=sched.target_ids, status="PENDING",
            procedure_snapshot=proc.body,
            created_at=now, triggered_by=f"schedule:{schedule_id}",
        )
        db.add(new_run)
        sched.last_run_at = now
        await db.commit()

    asyncio.create_task(_execute_procedure_run(run_id, body, targets, 120))
    logger.info("Schedule %s fired run %s", schedule_id, run_id)


async def _restore_schedules(scheduler: AsyncIOScheduler) -> None:
    """On startup, re-register all enabled schedules from the DB."""
    async with SessionLocal() as db:
        result = await db.execute(select(Schedule).where(Schedule.enabled.is_(True)))
        schedules = result.scalars().all()
    for sched in schedules:
        try:
            trigger = CronTrigger.from_crontab(sched.cron_expr, timezone="UTC")
            scheduler.add_job(
                _fire_schedule, trigger=trigger,
                args=[sched.schedule_id], id=sched.schedule_id, replace_existing=True,
            )
        except Exception as exc:
            logger.warning("Could not restore schedule %s: %s", sched.schedule_id, exc)
    logger.info("Restored %d schedules from DB", len(schedules))


def _schedule_out(s: Schedule) -> ScheduleOut:
    return ScheduleOut(
        schedule_id=s.schedule_id, procedure_id=s.procedure_id,
        target_ids=json.loads(s.target_ids), cron_expr=s.cron_expr,
        label=s.label, enabled=s.enabled,
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
        next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
        created_at=s.created_at.isoformat(),
    )


osiris_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)


async def require_valid_token_osiris(request: Request, db: AsyncSession = Depends(get_db)) -> ActiveToken:
    return await _validate_bearer_token(request, db, osiris_limiter)


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
ssh_exec_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)


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

    # Start the APScheduler and restore any enabled schedules from DB
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.start()
    app.state.scheduler = scheduler
    await _restore_schedules(scheduler)

    logger.info("Gateway startup complete. Environment=%s", settings.environment)
    yield

    scheduler.shutdown(wait=False)
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
                json=payload.model_dump(exclude_none=True),
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


# --------------------------------------------------------------------------
# SSH remote-execution routes
# --------------------------------------------------------------------------

@app.get("/api/ssh/targets", response_model=list[SshTargetOut])
async def list_ssh_targets(_token_row: ActiveToken = Depends(require_valid_token_ws)):
    """Return configured SSH targets (no secrets — credentials stay in env)."""
    targets = await asyncio.to_thread(load_ssh_targets)
    return [
        SshTargetOut(id=t.id, label=t.label, host=t.host, user=t.user, port=t.port, auth=t.auth, tags=t.tags)
        for t in targets
    ]


@app.post("/api/ssh/execute", response_model=SshJobOut, status_code=status.HTTP_202_ACCEPTED)
async def ssh_execute(
    payload: SshExecuteRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    token_row: ActiveToken = Depends(require_valid_token_ssh),
    db: AsyncSession = Depends(get_db),
):
    """Queue a procedure to run on one or more SSH targets. Returns
    immediately with a job_id; poll /api/ssh/jobs/{job_id} for status."""
    targets_map = {t.id: t for t in await asyncio.to_thread(load_ssh_targets)}

    unknown = [tid for tid in payload.target_ids if tid not in targets_map]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown target id(s): {unknown}",
        )

    resolved = [targets_map[tid] for tid in payload.target_ids]

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    job = SshJob(
        job_id=job_id,
        user_id=token_row.user_id,
        target_ids=json.dumps(payload.target_ids),
        procedure=payload.procedure,
        status="PENDING",
        results=None,
        created_at=now,
        completed_at=None,
    )
    db.add(job)
    await db.commit()

    background_tasks.add_task(
        _execute_ssh_job, job_id, resolved, payload.procedure, payload.timeout
    )

    logger.info(
        "SSH job %s queued: %d target(s), user_id=%d",
        job_id, len(resolved), token_row.user_id,
    )

    return _ssh_job_to_out(job)


@app.get("/api/ssh/jobs", response_model=list[SshJobOut])
async def list_ssh_jobs(
    limit: int = 50,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent SSH jobs (newest first)."""
    limit = max(1, min(limit, 200))
    result = await db.execute(
        select(SshJob).order_by(SshJob.created_at.desc()).limit(limit)
    )
    return [_ssh_job_to_out(j) for j in result.scalars().all()]


@app.get("/api/ssh/jobs/{job_id}", response_model=SshJobOut)
async def get_ssh_job(
    job_id: str,
    _token_row: ActiveToken = Depends(require_valid_token_ws),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SshJob).where(SshJob.job_id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _ssh_job_to_out(job)


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


# --------------------------------------------------------------------------
# Osiris Procedure CRUD
# --------------------------------------------------------------------------

@app.post("/api/procedures", response_model=ProcedureOut, status_code=status.HTTP_201_CREATED)
async def create_procedure(
    payload: ProcedureCreate,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    proc_id = str(uuid.uuid4())
    proc = Procedure(
        procedure_id=proc_id, user_id=_tok.user_id, name=payload.name,
        description=payload.description, body=payload.body.model_dump_json(),
        version=1, created_at=now, updated_at=now,
    )
    db.add(proc)
    await db.commit()
    return ProcedureOut(
        procedure_id=proc_id, name=proc.name, description=proc.description,
        version=1, step_count=len(payload.body.steps),
        created_at=now.isoformat(), updated_at=now.isoformat(),
    )


@app.get("/api/procedures", response_model=list[ProcedureOut])
async def list_procedures(
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Procedure).where(Procedure.user_id == _tok.user_id).order_by(Procedure.updated_at.desc()))
    procs = result.scalars().all()
    out = []
    for p in procs:
        try:
            body = json.loads(p.body)
            step_count = len(body.get("steps", []))
        except Exception:
            step_count = 0
        out.append(ProcedureOut(
            procedure_id=p.procedure_id, name=p.name, description=p.description,
            version=p.version, step_count=step_count,
            created_at=p.created_at.isoformat(), updated_at=p.updated_at.isoformat(),
        ))
    return out


@app.get("/api/procedures/{procedure_id}")
async def get_procedure(
    procedure_id: str,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Procedure).where(Procedure.procedure_id == procedure_id, Procedure.user_id == _tok.user_id))
    proc = result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Procedure not found")
    return {"procedure_id": proc.procedure_id, "name": proc.name, "description": proc.description,
            "version": proc.version, "body": json.loads(proc.body),
            "created_at": proc.created_at.isoformat(), "updated_at": proc.updated_at.isoformat()}


@app.put("/api/procedures/{procedure_id}", response_model=ProcedureOut)
async def update_procedure(
    procedure_id: str,
    payload: ProcedureUpdate,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Procedure).where(Procedure.procedure_id == procedure_id, Procedure.user_id == _tok.user_id))
    proc = result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Procedure not found")
    if payload.name is not None:
        proc.name = payload.name
    if payload.description is not None:
        proc.description = payload.description
    if payload.body is not None:
        proc.body = payload.body.model_dump_json()
    proc.version += 1
    proc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    body = json.loads(proc.body)
    return ProcedureOut(
        procedure_id=proc.procedure_id, name=proc.name, description=proc.description,
        version=proc.version, step_count=len(body.get("steps", [])),
        created_at=proc.created_at.isoformat(), updated_at=proc.updated_at.isoformat(),
    )


@app.delete("/api/procedures/{procedure_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_procedure(
    procedure_id: str,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Procedure).where(Procedure.procedure_id == procedure_id, Procedure.user_id == _tok.user_id))
    proc = result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Procedure not found")
    await db.delete(proc)
    await db.commit()


# --------------------------------------------------------------------------
# Osiris Run endpoints
# --------------------------------------------------------------------------

@app.post("/api/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    payload: RunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    proc_result = await db.execute(select(Procedure).where(Procedure.procedure_id == payload.procedure_id, Procedure.user_id == _tok.user_id))
    proc = proc_result.scalar_one_or_none()
    if not proc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Procedure not found")

    all_targets = load_ssh_targets()
    targets = [t for t in all_targets if t.id in payload.target_ids]
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No matching SSH targets found")

    body = OsirisBody.model_validate(json.loads(proc.body))
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    new_run = ProcedureRun(
        run_id=run_id, procedure_id=proc.procedure_id, user_id=_tok.user_id,
        target_ids=json.dumps(payload.target_ids), status="PENDING",
        procedure_snapshot=proc.body, created_at=now, triggered_by="manual",
    )
    db.add(new_run)
    await db.commit()

    background_tasks.add_task(_execute_procedure_run, run_id, body, targets, payload.timeout_per_step)
    return RunOut(
        run_id=run_id, procedure_id=proc.procedure_id, status="PENDING",
        target_ids=payload.target_ids, triggered_by="manual",
        results=None, created_at=now.isoformat(), completed_at=None,
    )


@app.get("/api/runs", response_model=list[RunOut])
async def list_runs(
    limit: int = 50,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProcedureRun).where(ProcedureRun.user_id == _tok.user_id)
        .order_by(ProcedureRun.created_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return [RunOut(
        run_id=r.run_id, procedure_id=r.procedure_id, status=r.status,
        target_ids=json.loads(r.target_ids), triggered_by=r.triggered_by,
        results=json.loads(r.results) if r.results else None,
        created_at=r.created_at.isoformat(),
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
    ) for r in runs]


@app.get("/api/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ProcedureRun).where(ProcedureRun.run_id == run_id, ProcedureRun.user_id == _tok.user_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunOut(
        run_id=run.run_id, procedure_id=run.procedure_id, status=run.status,
        target_ids=json.loads(run.target_ids), triggered_by=run.triggered_by,
        results=json.loads(run.results) if run.results else None,
        created_at=run.created_at.isoformat(),
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )


@app.get("/api/runs/{run_id}/logs", response_model=list[StepLogOut])
async def get_run_logs(
    run_id: str,
    target_id: Optional[str] = None,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    # Verify ownership
    run_result = await db.execute(select(ProcedureRun).where(ProcedureRun.run_id == run_id, ProcedureRun.user_id == _tok.user_id))
    if not run_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    q = select(RunStepLog).where(RunStepLog.run_id == run_id)
    if target_id:
        q = q.where(RunStepLog.target_id == target_id)
    q = q.order_by(RunStepLog.logged_at)
    result = await db.execute(q)
    logs = result.scalars().all()
    return [StepLogOut(
        id=l.id, run_id=l.run_id, target_id=l.target_id, step_id=l.step_id,
        step_name=l.step_name, action=l.action, command=l.command, reasoning=l.reasoning,
        stdout=l.stdout, stderr=l.stderr, exit_code=l.exit_code, status=l.status,
        attempt=l.attempt, fix_applied=l.fix_applied, duration_ms=l.duration_ms,
        logged_at=l.logged_at.isoformat(),
    ) for l in logs]


# --------------------------------------------------------------------------
# Schedule endpoints
# --------------------------------------------------------------------------

@app.post("/api/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreate,
    request: Request,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    proc_result = await db.execute(select(Procedure).where(Procedure.procedure_id == payload.procedure_id, Procedure.user_id == _tok.user_id))
    if not proc_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Procedure not found")

    try:
        trigger = CronTrigger.from_crontab(payload.cron_expr, timezone="UTC")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid cron expression: {exc}")

    sched_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    sched = Schedule(
        schedule_id=sched_id, user_id=_tok.user_id, procedure_id=payload.procedure_id,
        target_ids=json.dumps(payload.target_ids), cron_expr=payload.cron_expr,
        label=payload.label, enabled=payload.enabled, created_at=now,
    )
    db.add(sched)
    await db.commit()

    if payload.enabled:
        scheduler: AsyncIOScheduler = request.app.state.scheduler
        scheduler.add_job(_fire_schedule, trigger=trigger, args=[sched_id], id=sched_id, replace_existing=True)

    return _schedule_out(sched)


@app.get("/api/schedules", response_model=list[ScheduleOut])
async def list_schedules(
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Schedule).where(Schedule.user_id == _tok.user_id).order_by(Schedule.created_at.desc()))
    return [_schedule_out(s) for s in result.scalars().all()]


@app.delete("/api/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    _tok: ActiveToken = Depends(require_valid_token_osiris),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Schedule).where(Schedule.schedule_id == schedule_id, Schedule.user_id == _tok.user_id))
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    await db.delete(sched)
    await db.commit()
    scheduler: AsyncIOScheduler = request.app.state.scheduler
    if scheduler.get_job(schedule_id):
        scheduler.remove_job(schedule_id)

