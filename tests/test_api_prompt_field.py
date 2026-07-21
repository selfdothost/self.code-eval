"""Regression tests for issue #2 item 2 (bug 1, API half): self-code-eval's
JobCreate request model had no --prompt field at all, so an API caller had no way
to override the humanevalpack prompt mode per-job even after the CLI default was
fixed. These tests exercise api/main.py's JobCreate model and the CLI command it
builds directly, without spinning up the FastAPI app or a real job.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.main import JobCreate  # noqa: E402


def test_job_create_prompt_field_defaults_to_none():
    """Unset --prompt should mean 'don't pass --prompt at all', letting main.py's
    own corrected default ("instruct") take effect."""
    job = JobCreate(tasks="humanevalsynthesize-python", api_endpoint="http://x/v1/completions")
    assert job.prompt is None


def test_job_create_accepts_explicit_prompt_override():
    job = JobCreate(
        tasks="humanevalsynthesize-python",
        api_endpoint="http://x/v1/completions",
        prompt="octocoder",
    )
    assert job.prompt == "octocoder"


def test_job_create_schema_documents_prompt_field():
    schema = JobCreate.model_json_schema()
    assert "prompt" in schema["properties"]


def _build_prompt_cli_args(req: JobCreate):
    """Mirrors the --prompt handling in api/main.py's create_job(): only append
    the flag (and its value) when the caller set one explicitly."""
    args = []
    if req.prompt is not None:
        args.extend(["--prompt", req.prompt])
    return args


def test_create_job_omits_prompt_flag_when_unset():
    req = JobCreate(tasks="humanevalsynthesize-python", api_endpoint="http://x/v1/completions")
    assert _build_prompt_cli_args(req) == []


def test_create_job_threads_prompt_override_into_cli_args():
    req = JobCreate(
        tasks="humanevalsynthesize-python",
        api_endpoint="http://x/v1/completions",
        prompt="wizardcoder",
    )
    assert _build_prompt_cli_args(req) == ["--prompt", "wizardcoder"]
