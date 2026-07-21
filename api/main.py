"""
FastAPI server for controlling code-eval benchmark jobs.
Runs on port 8094, manages evaluation lifecycle and results.
"""

import asyncio
import html
import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import aiofiles
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ─── Constants ──────────────────────────────────────────────────────────

WORKSPACE = Path("/workspace")
RESULTS_DIR = WORKSPACE / "results"
LOGS_DIR = WORKSPACE / "logs"
JOBS_STATE_FILE = RESULTS_DIR / ".jobs.json"
APP_DIR = Path("/app")
PYTHON = "python3"

API_VERSION = "1.0.0"

# ─── Sanitization ──────────────────────────────────────────────────────

# Strict pattern for path-segment IDs (job_id, result_id)
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")

# Control chars to strip (keep \n \r \t for code readability)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Max string length for model-generated content (512 KB)
_MAX_STRING_LEN = 512 * 1024


def _validate_id(value: str, label: str = "ID") -> str:
    """Validate that a path-segment ID is safe (alphanumeric + hyphens only)."""
    if not _SAFE_ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: must be alphanumeric with hyphens/underscores, 1-64 chars",
        )
    return value


def _sanitize_string(s: str) -> str:
    """Sanitize a single string value from evaluation output.

    - Strips null bytes and non-printable control characters
    - HTML-escapes to prevent XSS when rendered in a browser
    - Truncates excessively long strings
    """
    # Remove dangerous control characters (keep \n, \r, \t)
    s = _CONTROL_CHAR_RE.sub("", s)
    # Truncate before expensive escaping
    if len(s) > _MAX_STRING_LEN:
        s = s[:_MAX_STRING_LEN] + "\n... [truncated]"
    # HTML-escape to neutralize any <script>, event handlers, etc.
    s = html.escape(s, quote=True)
    return s


def _sanitize_value(obj: Any) -> Any:
    """Recursively sanitize all string values in a JSON-like structure."""
    if isinstance(obj, str):
        return _sanitize_string(obj)
    elif isinstance(obj, dict):
        return {k: _sanitize_value(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_value(item) for item in obj]
    # Numbers, booleans, None pass through unchanged
    return obj


def _sanitize_log_line(line: str) -> str:
    """Sanitize a single log line (lighter touch — no HTML escaping needed
    since logs are served as text/plain, but strip control chars + null bytes)."""
    return _CONTROL_CHAR_RE.sub("", line)


# ─── Task Discovery ────────────────────────────────────────────────────

def _discover_tasks() -> List[str]:
    """Import the task registry and return all available task names."""
    try:
        import sys
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        from code_eval.tasks import ALL_TASKS
        return list(ALL_TASKS)
    except Exception as e:
        print(f"Warning: could not discover tasks: {e}")
        return []


_ALL_TASKS: List[str] = []


def _get_all_tasks() -> List[str]:
    """Lazy-load task list on first access."""
    global _ALL_TASKS
    if not _ALL_TASKS:
        _ALL_TASKS = _discover_tasks()
    return _ALL_TASKS


# ─── Enums and Models ───────────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCreate(BaseModel):
    tasks: str = Field(
        ...,
        description="Comma-separated task names or wildcards (e.g. 'humaneval', 'humaneval,mbpp', 'multiple-*')",
    )
    api_endpoint: str = Field(
        ...,
        description="OpenAI-compatible endpoint URL. Use /api/completions for text completion (recommended for code eval) or /api/chat/completions for chat mode.",
    )
    model: str = Field(
        default="default",
        description="Model name to identify in results",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key for the endpoint (sent as Bearer token)",
    )
    # Generation parameters
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=0, ge=0)
    n_samples: int = Field(
        default=1,
        ge=1,
        description="Number of completions per problem (for pass@k)",
    )
    max_length_generation: int = Field(
        default=512,
        ge=64,
        description="Maximum length of generated sequence (prompt+generation)",
    )
    batch_size: int = Field(default=1, ge=1)
    # Evaluation parameters
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of samples to evaluate (None = all)",
    )
    limit_start: int = Field(default=0, ge=0)
    allow_code_execution: bool = Field(
        default=True,
        description="Allow tasks that execute generated code",
    )
    # Output control
    save_generations: bool = Field(default=True)
    save_references: bool = Field(default=True)
    do_sample: bool = Field(default=True)
    seed: int = Field(default=0)
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Prompt template for HumanEvalPack task variants "
            "(humanevalsynthesize-*/humanevalfixtests-*/humanevalfixdocs-*/"
            "humanevalexplainsynthesize-*). Ignored by tasks that don't accept a "
            "--prompt argument. Defaults to 'instruct' if unset. Other supported "
            "values include: continue (HumanEvalSynthesize only), octocoder, "
            "octogeex, starchat, starcodercommit, instructcodet5p, wizardcoder, "
            "codellama, codellama-70b, deepseek, tulu, gritlm, zephyr, yi, "
            "starchat2, codeqwen, codegemma, aurora-m."
        ),
    )

    class Config:
        json_schema_extra = {
            "example": {
                "tasks": "humaneval",
                "api_endpoint": "http://selfUI:8080/api/completions",
                "model": "Qwen-7B",
                "temperature": 0.2,
                "n_samples": 1,
                "limit": 10,
            }
        }


class Job(BaseModel):
    job_id: str
    status: JobStatus
    tasks: str
    model: str
    api_endpoint: str
    pid: Optional[int] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    log_file: str
    results_file: str
    details_file: Optional[str] = None
    error_message: Optional[str] = None
    config: Dict[str, Any] = {}


class HealthResponse(BaseModel):
    status: str
    running_jobs: int
    jobs_total: int
    available_tasks: int
    api_version: str


class TaskInfo(BaseModel):
    name: str
    category: str


# ─── State ──────────────────────────────────────────────────────────────

_jobs: Dict[str, Job] = {}
_processes: Dict[str, subprocess.Popen] = {}

app = FastAPI(title="BigCode Evaluation API", version=API_VERSION)


# ─── Persistence ────────────────────────────────────────────────────────

def _ensure_dirs():
    for d in [RESULTS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _load_jobs():
    global _jobs
    if JOBS_STATE_FILE.exists():
        data = json.loads(JOBS_STATE_FILE.read_text())
        for job_id, job_data in data.items():
            try:
                job_data["created_at"] = datetime.fromisoformat(job_data["created_at"])
                if job_data.get("started_at"):
                    job_data["started_at"] = datetime.fromisoformat(job_data["started_at"])
                if job_data.get("finished_at"):
                    job_data["finished_at"] = datetime.fromisoformat(job_data["finished_at"])
                job = Job(**job_data)
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.FAILED
                    job.error_message = "Process lost on restart"
                    job.finished_at = datetime.now()
                _jobs[job_id] = job
            except Exception as e:
                print(f"Failed to load job {job_id}: {e}")


def _save_jobs():
    tmp = JOBS_STATE_FILE.with_suffix(".tmp")
    data = {jid: j.model_dump(mode="json") for jid, j in _jobs.items()}
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(JOBS_STATE_FILE)


# ─── Background Polling ────────────────────────────────────────────────

async def _poll_jobs():
    while True:
        await asyncio.sleep(5)
        changed = False
        for job_id, job in list(_jobs.items()):
            if job.status != JobStatus.RUNNING:
                continue

            proc = _processes.get(job_id)
            if not proc:
                continue

            rc = proc.poll()
            if rc is not None:
                job.exit_code = rc
                job.finished_at = datetime.now()
                job.status = JobStatus.COMPLETED if rc == 0 else JobStatus.FAILED
                if rc != 0:
                    # Try to extract error from log tail
                    try:
                        log_lines = Path(job.log_file).read_text().splitlines()
                        tail = log_lines[-5:] if len(log_lines) >= 5 else log_lines
                        job.error_message = _sanitize_string("\n".join(tail))
                    except Exception:
                        job.error_message = f"Process exited with code {rc}"
                else:
                    # Try to find the details file
                    _discover_details_file(job)
                del _processes[job_id]
                changed = True

        if changed:
            _save_jobs()


def _discover_details_file(job: Job):
    """Look for a details JSON file produced by the evaluation."""
    try:
        prefix = Path(job.results_file).stem.replace("-results", "-details")
        for path in RESULTS_DIR.glob(f"{prefix}_*.json"):
            job.details_file = str(path)
            break
    except Exception:
        pass


@app.on_event("startup")
async def startup_event():
    _ensure_dirs()
    _load_jobs()
    asyncio.create_task(_poll_jobs())


# ─── Task Discovery Endpoints ──────────────────────────────────────────

def _categorize_task(name: str) -> str:
    """Assign a human-readable category to a task name."""
    if name.startswith("multiple-"):
        return "multilingual"
    if name.startswith("humanevalpack-"):
        return "humanevalpack"
    if name.startswith("apps-"):
        return "apps"
    if name.startswith("ds1000"):
        return "ds1000"
    if name.startswith("codexglue_code_to_text"):
        return "code-to-text"
    if name.startswith("codexglue_text_to_text"):
        return "text-to-text"
    if name.startswith("pal-") or name.startswith("gsm"):
        return "math"
    if name.startswith("recode"):
        return "robustness"
    if name.startswith("santacoder") or name.startswith("starcoder"):
        return "infill"
    if name.startswith("instruct"):
        return "instruction"
    if "humaneval" in name:
        return "humaneval"
    if "mbpp" in name:
        return "mbpp"
    return "other"


@app.get("/api/tasks")
def list_tasks() -> List[TaskInfo]:
    """List all available benchmark tasks."""
    return [
        TaskInfo(name=name, category=_categorize_task(name))
        for name in _get_all_tasks()
    ]


@app.get("/api/tasks/categories")
def list_task_categories() -> Dict[str, List[str]]:
    """List tasks grouped by category."""
    categories: Dict[str, List[str]] = {}
    for name in _get_all_tasks():
        cat = _categorize_task(name)
        categories.setdefault(cat, []).append(name)
    return categories


# ─── Jobs Endpoints ────────────────────────────────────────────────────

@app.post("/api/jobs", status_code=201)
def create_job(req: JobCreate) -> Job:
    """Start a new evaluation job."""
    # Validate string inputs — reject null bytes and shell metacharacters
    # (subprocess uses list args so no shell injection, but defense-in-depth)
    _string_fields = [("tasks", req.tasks), ("model", req.model), ("api_endpoint", req.api_endpoint)]
    if req.prompt is not None:
        _string_fields.append(("prompt", req.prompt))
    for field_name, value in _string_fields:
        if "\x00" in value:
            raise HTTPException(status_code=400, detail=f"Invalid {field_name}: contains null bytes")
        if any(c in value for c in [";", "|", "&", "`", "$", "(", ")", "\n", "\r"]):
            raise HTTPException(status_code=400, detail=f"Invalid {field_name}: contains disallowed characters")
    if req.api_key and ("\x00" in req.api_key or "\n" in req.api_key):
        raise HTTPException(status_code=400, detail="Invalid api_key")

    # Check if any job is already running
    for job in _jobs.values():
        if job.status == JobStatus.RUNNING:
            raise HTTPException(
                status_code=409,
                detail=f"An evaluation job is already running (job_id: {job.job_id})",
            )

    # Validate tasks against registry
    all_tasks = _get_all_tasks()
    import fnmatch

    requested = req.tasks.split(",")
    matched = set()
    for pattern in requested:
        pattern = pattern.strip()
        matches = fnmatch.filter(all_tasks, pattern)
        if not matches:
            raise HTTPException(
                status_code=400,
                detail=f"No tasks match pattern '{pattern}'. Use GET /api/tasks to see available tasks.",
            )
        matched.update(matches)

    job_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results_file = str(RESULTS_DIR / f"{job_id}-results.json")
    details_file_base = str(RESULTS_DIR / f"{job_id}-details")
    log_file = str(LOGS_DIR / f"{job_id}.log")
    generations_path = str(RESULTS_DIR / f"{job_id}-generations.json")
    references_path = str(RESULTS_DIR / f"{job_id}-references.json")

    # Build CLI command
    cmd = [
        PYTHON,
        str(APP_DIR / "main.py"),
        "--tasks", req.tasks,
        "--api_endpoint", req.api_endpoint,
        "--model", req.model,
        "--temperature", str(req.temperature),
        "--top_p", str(req.top_p),
        "--top_k", str(req.top_k),
        "--n_samples", str(req.n_samples),
        "--max_length_generation", str(req.max_length_generation),
        "--batch_size", str(req.batch_size),
        "--limit_start", str(req.limit_start),
        "--seed", str(req.seed),
        "--metric_output_path", results_file,
        "--save_generations_path", generations_path,
        "--save_references_path", references_path,
        "--save_details_path", details_file_base,
    ]

    if req.api_key:
        cmd.extend(["--api_key", req.api_key])
    if req.limit is not None:
        cmd.extend(["--limit", str(req.limit)])
    if req.allow_code_execution:
        cmd.append("--allow_code_execution")
    if req.save_generations:
        cmd.append("--save_generations")
    if req.save_references:
        cmd.append("--save_references")
    if req.do_sample:
        cmd.append("--do_sample")
    if req.prompt is not None:
        cmd.extend(["--prompt", req.prompt])

    # Live events file for streaming prompt/response pairs
    live_events_file = str(LOGS_DIR / f"{job_id}.events.jsonl")

    # Open log file
    try:
        log_fh = open(log_file, "w")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open log: {e}")

    # Launch process
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["BIGCODE_LIVE_EVENTS_PATH"] = live_events_file

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(APP_DIR),
        )
    except Exception as e:
        log_fh.close()
        raise HTTPException(status_code=500, detail=f"Failed to start evaluation: {e}")

    config = req.model_dump()

    job = Job(
        job_id=job_id,
        status=JobStatus.RUNNING,
        tasks=req.tasks,
        model=req.model,
        api_endpoint=req.api_endpoint,
        pid=proc.pid,
        created_at=datetime.now(),
        started_at=datetime.now(),
        log_file=log_file,
        results_file=results_file,
        config=config,
    )

    _jobs[job_id] = job
    _processes[job_id] = proc
    _save_jobs()

    return job


@app.get("/api/jobs")
def list_jobs(
    status: Optional[JobStatus] = Query(None, description="Filter by status"),
) -> List[Job]:
    """List all jobs, optionally filtered by status."""
    jobs = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    if status:
        jobs = [j for j in jobs if j.status == status]
    return jobs


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Job:
    """Get job details."""
    _validate_id(job_id, "job_id")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: str, tail: int = Query(100, ge=1, le=10000), stream: bool = Query(False)
):
    """Get job logs. Use stream=true for live tailing."""
    _validate_id(job_id, "job_id")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    log_path = Path(job.log_file)
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    if not stream:
        try:
            lines = log_path.read_text().splitlines()
            sanitized = [_sanitize_log_line(l) for l in lines[-tail:]]
            return {"lines": sanitized, "total_lines": len(lines)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def generate():
        async with aiofiles.open(log_path, "r") as f:
            await f.seek(0, 2)
            while True:
                line = await f.readline()
                if line:
                    yield _sanitize_log_line(line)
                else:
                    # Stop streaming if job is done
                    j = _jobs.get(job_id)
                    if j and j.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                        remaining = await f.readline()
                        while remaining:
                            yield _sanitize_log_line(remaining)
                            remaining = await f.readline()
                        break
                    await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/plain")


@app.get("/api/jobs/{job_id}/live")
async def get_job_live(job_id: str):
    """Stream prompt/response pairs as SSE events during a running evaluation."""
    _validate_id(job_id, "job_id")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    events_path = LOGS_DIR / f"{job_id}.events.jsonl"

    async def generate():
        lines_sent = 0
        while True:
            # Read any new lines from the events file
            if events_path.exists():
                try:
                    async with aiofiles.open(events_path, "r") as f:
                        all_lines = await f.readlines()
                    new_lines = all_lines[lines_sent:]
                    for line in new_lines:
                        line = line.strip()
                        if line:
                            yield f"data: {line}\n\n"
                            lines_sent += 1
                except Exception:
                    pass

            # Check if job is done
            j = _jobs.get(job_id)
            if j and j.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                # Flush any remaining lines
                if events_path.exists():
                    try:
                        async with aiofiles.open(events_path, "r") as f:
                            all_lines = await f.readlines()
                        for line in all_lines[lines_sent:]:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                    except Exception:
                        pass
                yield f"event: done\ndata: {{\"status\": \"{j.status}\"}}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    """Cancel a running job."""
    _validate_id(job_id, "job_id")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Job is not running")

    proc = _processes.get(job_id)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        del _processes[job_id]

    job.status = JobStatus.CANCELLED
    job.finished_at = datetime.now()
    _save_jobs()

    return {"status": "cancelled", "job_id": job_id}


@app.delete("/api/jobs/{job_id}/purge")
def purge_job(job_id: str):
    """Delete a job and all its associated files (logs, results, generations)."""
    _validate_id(job_id, "job_id")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Cannot purge a running job. Cancel it first.")

    deleted_files = []
    # Remove log file
    log_path = Path(job.log_file)
    if log_path.exists():
        log_path.unlink()
        deleted_files.append(str(log_path))

    # Remove results file
    results_path = Path(job.results_file)
    if results_path.exists():
        results_path.unlink()
        deleted_files.append(str(results_path))

    # Remove details file
    if job.details_file:
        details_path = Path(job.details_file)
        if details_path.exists():
            details_path.unlink()
            deleted_files.append(str(details_path))

    # Remove any associated generation/reference files
    for pattern in [f"{job_id}-generations*.json", f"{job_id}-references*.json", f"{job_id}-details*.json"]:
        for f in RESULTS_DIR.glob(pattern):
            f.unlink()
            deleted_files.append(str(f))

    del _jobs[job_id]
    _save_jobs()

    return {"purged": True, "job_id": job_id, "files_removed": deleted_files}


# ─── Results Endpoints ──────────────────────────────────────────────────

@app.get("/api/results")
def list_results() -> List[Dict[str, Any]]:
    """List all evaluation results (from completed jobs)."""
    results = []
    if not RESULTS_DIR.is_dir():
        return results

    for path in sorted(RESULTS_DIR.glob("*-results.json")):
        try:
            data = json.loads(path.read_text())
            config = data.get("config", {})
            scores = {k: v for k, v in data.items() if k != "config"}

            # Find the job that produced this result
            job_id = path.stem.replace("-results", "")
            job = _jobs.get(job_id)

            results.append(_sanitize_value({
                "id": path.stem,
                "job_id": job_id,
                "filename": path.name,
                "model": config.get("model", "unknown"),
                "tasks": config.get("tasks", "unknown"),
                "scores": scores,
                "config": config,
                "created_at": job.created_at.isoformat() if job else None,
                "finished_at": job.finished_at.isoformat() if job and job.finished_at else None,
            }))
        except Exception as e:
            print(f"Failed to parse {path}: {e}")

    return sorted(results, key=lambda r: r.get("finished_at") or "", reverse=True)


@app.get("/api/results/{result_id}")
def get_result(result_id: str) -> Dict[str, Any]:
    """Get full evaluation results for a specific run."""
    _validate_id(result_id, "result_id")
    # Support both "abc123-results" and "abc123" as result_id
    if not result_id.endswith("-results"):
        result_id = f"{result_id}-results"

    path = RESULTS_DIR / f"{result_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Result not found")

    try:
        data = json.loads(path.read_text())
        return _sanitize_value(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read result: {e}")


@app.get("/api/results/{result_id}/details")
def get_result_details(result_id: str) -> List[Dict[str, Any]]:
    """Get per-problem details for a specific evaluation run."""
    _validate_id(result_id, "result_id")
    base_id = result_id.replace("-results", "")

    details = []
    for path in sorted(RESULTS_DIR.glob(f"{base_id}-details_*.json")):
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                details.extend(data)
            else:
                details.append(data)
        except Exception as e:
            print(f"Failed to parse details {path}: {e}")

    if not details:
        raise HTTPException(status_code=404, detail="Details not found for this evaluation run")

    return _sanitize_value(details)


@app.get("/api/results/{result_id}/generations")
def get_result_generations(result_id: str):
    """Get the raw code generations for a specific evaluation run."""
    _validate_id(result_id, "result_id")
    base_id = result_id.replace("-results", "")

    for path in sorted(RESULTS_DIR.glob(f"{base_id}-generations*.json")):
        try:
            data = json.loads(path.read_text())
            return _sanitize_value(data)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read generations: {e}")

    raise HTTPException(status_code=404, detail="Generations not found for this evaluation run")


# ─── Health Endpoint ────────────────────────────────────────────────────

@app.get("/health")
def health() -> HealthResponse:
    running_count = sum(1 for j in _jobs.values() if j.status == JobStatus.RUNNING)
    return HealthResponse(
        status="ok",
        running_jobs=running_count,
        jobs_total=len(_jobs),
        available_tasks=len(_get_all_tasks()),
        api_version=API_VERSION,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8094, log_level="info")
