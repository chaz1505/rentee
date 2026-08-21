import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


CODEX_PROVIDER = "codex_local_cli"
CODEX_REPOSITORY = "chaz1505/rentee"
CODEX_REPOSITORY_URL = "https://github.com/chaz1505/rentee.git"
CODEX_BASE_BRANCH = "main"
WORKSPACE_ROOT = Path("/tmp/rentee-codex")
AUTH_ROOT = Path("/tmp/rentee-codex-auth")
WORKSPACE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 120
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


def _safe_identifier(value, fallback="run"):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or fallback)).strip("-_")
    return cleaned[:100] or fallback


def create_codex_task_id(run_id):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"codex_{_safe_identifier(run_id)}_{timestamp}_{uuid.uuid4().hex[:8]}"


def _execution_timeout():
    raw = os.environ.get(
        "CODEX_EXEC_TIMEOUT_SECONDS", str(DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS)
    )
    try:
        value = int(raw)
    except ValueError as error:
        raise CodexSubmissionError(
            "CODEX_EXEC_TIMEOUT_SECONDS must be a positive integer."
        ) from error
    if value <= 0:
        raise CodexSubmissionError(
            "CODEX_EXEC_TIMEOUT_SECONDS must be a positive integer."
        )
    return value


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
    ).strip()
    if len(sanitized) > MAX_CLI_DIAGNOSTIC_LENGTH:
        sanitized = sanitized[:MAX_CLI_DIAGNOSTIC_LENGTH] + "…[truncated]"
    return sanitized


def _run(command, *, cwd=None, timeout=GIT_TIMEOUT_SECONDS, input_text=None, env=None):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CodexSubmissionError(
            f"Command could not be completed: {type(error).__name__}"
        ) from error


def _require_success(result, action, sensitive_values=()):
    if result.returncode == 0:
        return result
    safe_stdout = _sanitize_cli_output(result.stdout, sensitive_values)
    safe_stderr = _sanitize_cli_output(result.stderr, sensitive_values)
    print(f"[CODEX] Command exited with code {result.returncode}: {action}", flush=True)
    if safe_stdout:
        print(f"[CODEX] stdout: {safe_stdout}", flush=True)
    if safe_stderr:
        print(f"[CODEX] stderr: {safe_stderr}", flush=True)
    detail = safe_stderr or safe_stdout or "no command error output"
    raise CodexSubmissionError(f"{action} failed: {detail}")


def _codex_environment(auth_home):
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(auth_home)
    return environment


def _prepare_api_key_authentication(codex_path, task_id):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise CodexSubmissionError("OPENAI_API_KEY is not configured.")

    auth_home = AUTH_ROOT / task_id
    AUTH_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    AUTH_ROOT.chmod(0o700)
    auth_home.mkdir(mode=0o700)
    auth_home.chmod(0o700)
    codex_env = _codex_environment(auth_home)

    version = _run([codex_path, "--version"], env=codex_env)
    _require_success(version, "Codex CLI version inspection", (api_key,))
    safe_version = _sanitize_cli_output(version.stdout or version.stderr, (api_key,))
    print(f"[CODEX] Codex CLI version: {safe_version}", flush=True)
    print("[CODEX] OPENAI_API_KEY present: yes", flush=True)
    print(f"[CODEX] OPENAI_API_KEY length: {len(api_key)}", flush=True)

    login_help = _run([codex_path, "login", "--help"], env=codex_env)
    _require_success(login_help, "Codex login interface inspection", (api_key,))
    if "--with-api-key" not in (login_help.stdout or ""):
        raise CodexSubmissionError(
            "Installed Codex CLI does not support non-interactive API-key login."
        )

    print("[CODEX] Preparing API-key authentication...", flush=True)
    login = _run(
        [codex_path, "login", "--with-api-key"],
        input_text=api_key,
        env=codex_env,
    )
    _require_success(login, "Codex API-key login", (api_key,))

    status = _run([codex_path, "login", "status"], env=codex_env)
    _require_success(status, "Codex login status", (api_key,))
    safe_status = _sanitize_cli_output(
        status.stdout or status.stderr, (api_key,)
    )
    print(f"[CODEX] Codex login status: {safe_status}", flush=True)
    normalized_status = safe_status.casefold()
    if "chatgpt" in normalized_status or "api key" not in normalized_status:
        raise CodexSubmissionError(
            "Codex CLI did not confirm API-key authentication."
        )
    print("[CODEX] API-key authentication ready.", flush=True)
    return codex_env, auth_home


def _codex_failure_message(stdout, stderr):
    combined = f"{stderr or ''}\n{stdout or ''}".casefold()
    if re.search(r"(?:http(?: error)?[: ]+|status(?: code)?[: ]+)401\b|401 unauthorized", combined):
        return "Codex API-key authentication was rejected by OpenAI."
    if re.search(r"(?:http(?: error)?[: ]+|status(?: code)?[: ]+)403\b|403 forbidden", combined):
        return "Codex API access was forbidden by OpenAI (HTTP 403)."
    if (
        re.search(r"(?:http(?: error)?[: ]+|status(?: code)?[: ]+)404\b", combined)
        or "model_not_found" in combined
        or "model not found" in combined
    ):
        return "The configured Codex model is unavailable or was not found."
    if any(term in combined for term in (
        "insufficient_quota", "quota exceeded", "billing", "rate limit"
    )):
        return "Codex API quota, billing, or rate-limit access was rejected."
    return None


def check_codex_authentication(codex_path, codex_env, timeout=60):
    """Run an optional read-only API smoke test without touching the repository."""
    prompt = "Reply with exactly: CODEX_AUTH_OK"
    result = _run(
        [
            codex_path, "exec", "--ignore-user-config", "--ephemeral",
            "--sandbox", "read-only", "--skip-git-repo-check", "-",
        ],
        cwd=codex_env["CODEX_HOME"],
        timeout=timeout,
        input_text=prompt,
        env=codex_env,
    )
    if result.returncode != 0:
        safe_stdout = _sanitize_cli_output(result.stdout, (prompt,))
        safe_stderr = _sanitize_cli_output(result.stderr, (prompt,))
        classified = _codex_failure_message(safe_stdout, safe_stderr)
        raise CodexSubmissionError(
            classified or "Codex authentication smoke test failed."
        )
    return {"authenticated": True, "mode": "api_key"}


def cleanup_old_workspaces(active_task_id=None, now=None):
    if not WORKSPACE_ROOT.exists():
        return []
    current_time = time.time() if now is None else now
    removed = []
    for workspace in WORKSPACE_ROOT.iterdir():
        if not workspace.is_dir() or workspace.name == active_task_id:
            continue
        try:
            age = current_time - workspace.stat().st_mtime
            if age > WORKSPACE_TTL_SECONDS:
                shutil.rmtree(workspace)
                removed.append(workspace.name)
        except OSError:
            continue
    return removed


def _changed_files(status_output):
    files = []
    for line in (status_output or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def submit_codex_fix(
    prompt, run_id, benchmark_run_id, environment, task_id=None
):
    codex_path = shutil.which("codex")
    git_path = shutil.which("git")
    if not codex_path:
        raise CodexSubmissionError("Codex CLI is not installed in the Render runtime.")
    if not git_path:
        raise CodexSubmissionError("git is not installed in the Render runtime.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise CodexSubmissionError("OPENAI_API_KEY is not configured.")

    task_id = task_id or create_codex_task_id(run_id)
    workspace = WORKSPACE_ROOT / task_id
    branch = (
        f"codex/benchmark-{_safe_identifier(run_id)}-"
        f"{task_id.rsplit('_', 1)[-1]}"
    )
    complete_prompt = build_codex_prompt(prompt)
    _reject_configured_secrets(complete_prompt)
    cleanup_old_workspaces(active_task_id=task_id)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if workspace.exists():
        raise CodexSubmissionError("Codex task workspace already exists.")

    print(f"[CODEX] Task ID: {task_id}", flush=True)
    print("[CODEX] Creating workspace...", flush=True)
    print(f"[CODEX] Cloning {CODEX_REPOSITORY}...", flush=True)
    clone = _run([
        git_path, "clone", "--depth", "1", "--branch", CODEX_BASE_BRANCH,
        CODEX_REPOSITORY_URL, str(workspace),
    ])
    _require_success(clone, "Repository clone")
    if not (workspace / ".git").is_dir():
        raise CodexSubmissionError("Repository clone completed without a .git directory.")

    revision = _run([git_path, "rev-parse", "HEAD"], cwd=workspace)
    _require_success(revision, "Base commit lookup")
    base_commit = revision.stdout.strip()
    if not base_commit:
        raise CodexSubmissionError("Base commit lookup returned no commit.")
    print(f"[CODEX] Base commit: {base_commit}", flush=True)

    checkout = _run([git_path, "checkout", "-b", branch], cwd=workspace)
    _require_success(checkout, "Task branch creation")
    print(f"[CODEX] Created branch: {branch}", flush=True)

    codex_env = None
    auth_home = None
    try:
        codex_env, auth_home = _prepare_api_key_authentication(
            codex_path, task_id
        )
        if os.environ.get("CODEX_AUTH_SMOKE_TEST") == "1":
            print("[CODEX] Running API authentication smoke test...", flush=True)
            check_codex_authentication(codex_path, codex_env)
            print("[CODEX] API authentication smoke test succeeded.", flush=True)
        print("[CODEX] Starting local codex exec...", flush=True)
        execution = _run(
            [
                codex_path, "exec", "--ignore-user-config", "--ephemeral",
                "--sandbox", "workspace-write", "-",
            ],
            cwd=workspace,
            timeout=_execution_timeout(),
            input_text=complete_prompt,
            env=codex_env,
        )
    finally:
        if auth_home is not None:
            shutil.rmtree(auth_home, ignore_errors=True)
    if execution.returncode != 0:
        safe_stdout = _sanitize_cli_output(
            execution.stdout, (complete_prompt, prompt)
        )
        safe_stderr = _sanitize_cli_output(
            execution.stderr, (complete_prompt, prompt)
        )
        print(f"[CODEX] Codex CLI exited with code {execution.returncode}", flush=True)
        if safe_stdout:
            print(f"[CODEX] stdout: {safe_stdout}", flush=True)
        if safe_stderr:
            print(f"[CODEX] stderr: {safe_stderr}", flush=True)
        classified = _codex_failure_message(safe_stdout, safe_stderr)
        detail = safe_stderr or safe_stdout or "no CLI error output"
        raise CodexSubmissionError(
            classified or f"Local Codex execution failed: {detail}"
        )

    safe_final_output = _sanitize_cli_output(
        execution.stdout or execution.stderr, (complete_prompt, prompt)
    )
    if safe_final_output:
        print(f"[CODEX] Final response:\n{safe_final_output}", flush=True)

    status = _run([git_path, "status", "--porcelain"], cwd=workspace)
    _require_success(status, "Workspace status lookup")
    changed_files = _changed_files(status.stdout)
    print("[CODEX] Codex execution complete.", flush=True)
    print(
        f"[CODEX] Changes detected: {'yes' if changed_files else 'no'}",
        flush=True,
    )
    if changed_files:
        print(f"[CODEX] Changed files: {', '.join(changed_files)}", flush=True)
    return {
        "task_id": task_id,
        "task_id_source": "rentee_generated",
        "status": "completed",
        "provider": CODEX_PROVIDER,
        "repository": CODEX_REPOSITORY,
        "base_branch": CODEX_BASE_BRANCH,
        "branch": branch,
        "base_commit": base_commit,
        "workspace": str(workspace),
        "changes_detected": bool(changed_files),
        "changed_files": changed_files,
        "benchmark_run_id": benchmark_run_id,
        "environment": environment,
    }
