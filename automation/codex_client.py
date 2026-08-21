import os
import re
import shutil
import subprocess
from datetime import datetime, timezone


CODEX_PROVIDER = "codex_cloud_cli"
CODEX_REPOSITORY = "chaz1505/rentee"
CODEX_BASE_BRANCH = "main"
SUBMISSION_TIMEOUT_SECONDS = 60
MAX_CLI_DIAGNOSTIC_LENGTH = 2000

EXECUTION_WRAPPER = """You are operating autonomously on the Rentee repository.

Repository: chaz1505/rentee
Base branch: main

Inspect the current repository before editing.
Implement the supplied benchmark fix prompt.
Do not special-case Sofia or the exact benchmark wording.
Do not modify benchmark thresholds to manufacture a pass.
Preserve live-data safety and existing grounding constraints.
Add/update focused tests.
Run relevant tests before finishing.
Do not push directly to main.
Do not merge anything.

At completion, provide a concise structured summary of:
- root cause found
- files changed
- tests run
- test results
- remaining concerns

The complete benchmark fix prompt follows exactly below.

--- BEGIN COMPLETE BENCHMARK FIX PROMPT ---
"""


class CodexSubmissionError(RuntimeError):
    pass


def build_codex_prompt(fix_prompt):
    return (
        f"{EXECUTION_WRAPPER}\n{fix_prompt}"
        "\n--- END COMPLETE BENCHMARK FIX PROMPT ---\n"
    )


def _reject_configured_secrets(prompt):
    for name in (
        "BUBBLE_API_TOKEN", "BENCHMARK_API_KEY", "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
    ):
        secret = os.environ.get(name)
        if secret and secret in prompt:
            raise CodexSubmissionError(
                f"Codex prompt contains configured secret material ({name})."
            )


def _sanitize_cli_output(value, sensitive_values=()):
    sanitized = str(value or "")
    secrets = [
        os.environ.get(name)
        for name in (
            "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "BENCHMARK_API_KEY",
            "BUBBLE_API_TOKEN",
        )
    ]
    secrets.extend(sensitive_values)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(str(secret), "[REDACTED]")
    sanitized = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1[REDACTED]",
        sanitized,
    )
    sanitized = sanitized.strip()
    if len(sanitized) > MAX_CLI_DIAGNOSTIC_LENGTH:
        sanitized = sanitized[:MAX_CLI_DIAGNOSTIC_LENGTH] + "…[truncated]"
    return sanitized


def _run(command, **kwargs):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SUBMISSION_TIMEOUT_SECONDS,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CodexSubmissionError(
            f"Codex CLI could not be started: {type(error).__name__}"
        ) from error


def _ensure_authenticated(codex_path):
    status = _run([codex_path, "login", "status"])
    if status.returncode == 0:
        return
    access_token = os.environ.get("CODEX_ACCESS_TOKEN")
    api_key = os.environ.get("OPENAI_API_KEY")
    if access_token:
        login = _run(
            [codex_path, "login", "--with-access-token"], input=access_token
        )
    elif api_key:
        login = _run([codex_path, "login", "--with-api-key"], input=api_key)
    else:
        raise CodexSubmissionError(
            "Codex CLI is not authenticated and no supported credential is configured."
        )
    if login.returncode != 0:
        raise CodexSubmissionError("Codex CLI authentication failed.")


def _task_id(output):
    patterns = (
        r"\b(task_[A-Za-z0-9_-]+)\b",
        r"\b([0-9a-f]{8}-[0-9a-f-]{27,})\b",
        r"/tasks/([A-Za-z0-9_-]+)",
        r"\bTask ID:\s*([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, output or "", re.IGNORECASE)
        if match:
            return match.group(1), "codex_cloud"
    return None, None


def submit_codex_fix(prompt, run_id, benchmark_run_id, environment):
    codex_path = shutil.which("codex")
    if not codex_path:
        raise CodexSubmissionError(
            "Codex CLI is not installed in the Render runtime."
        )
    cloud_environment = os.environ.get("CODEX_CLOUD_ENV_ID", "").strip()
    if not cloud_environment:
        raise CodexSubmissionError("CODEX_CLOUD_ENV_ID is not configured.")
    _ensure_authenticated(codex_path)
    complete_prompt = build_codex_prompt(prompt)
    _reject_configured_secrets(complete_prompt)
    submission = _run([
        codex_path,
        "cloud",
        "exec",
        "--env",
        cloud_environment,
        complete_prompt,
    ])
    if submission.returncode != 0:
        safe_stdout = _sanitize_cli_output(
            submission.stdout, (complete_prompt, prompt)
        )
        safe_stderr = _sanitize_cli_output(
            submission.stderr, (complete_prompt, prompt)
        )
        print(
            f"[CODEX] Codex CLI exited with code {submission.returncode}",
            flush=True,
        )
        if safe_stdout:
            print(f"[CODEX] stdout: {safe_stdout}", flush=True)
        if safe_stderr:
            print(f"[CODEX] stderr: {safe_stderr}", flush=True)
        concise_error = safe_stderr or safe_stdout or "no CLI error output"
        raise CodexSubmissionError(
            f"Codex cloud task submission failed: {concise_error}"
        )
    output = "\n".join((submission.stdout or "", submission.stderr or ""))
    task_id, task_id_source = _task_id(output)
    if not task_id:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_run_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(run_id or "run"))
        task_id = f"codex_{safe_run_id}_{timestamp}"
        task_id_source = "rentee_generated"
    return {
        "task_id": task_id,
        "task_id_source": task_id_source,
        "status": "submitted",
        "provider": CODEX_PROVIDER,
        "repository": CODEX_REPOSITORY,
        "base_branch": CODEX_BASE_BRANCH,
        "benchmark_run_id": benchmark_run_id,
        "environment": environment,
    }
