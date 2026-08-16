import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests


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
GITHUB_API_URL = "https://api.github.com"
GITHUB_REQUEST_TIMEOUT_SECONDS = 30
GIT_AUTOMATION_NAME = "Rentee Codex"
GIT_AUTOMATION_EMAIL = "codex@rentee.asia"
CODEX_CHILD_STRIPPED_ENV_VARS = (
    "BENCHMARK_API_KEY",
    "BUBBLE_API_TOKEN",
    "GITHUB_TOKEN",
)

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

AUTOMATED FIX EXECUTION RULES

You are running inside Rentee's automated benchmark-fix pipeline.

You MAY:
- inspect repository files;
- edit repository files;
- add or update focused unit tests;
- run unit tests that do not call live Rentee endpoints;
- run static checks, compilation checks, and local mocked tests.

You MUST NOT:
- run tests/run_benchmark.py;
- run any benchmark runner;
- call /admin/run_benchmark;
- call /admin/benchmark/*;
- call /chat_stream;
- send HTTP requests to rentee.asia or rentee-2.onrender.com;
- invoke the live or development Rentee application;
- create another BenchmarkRun;
- recursively invoke the benchmark-fix workflow;
- perform end-to-end or integration tests that make external Rentee requests;
- push, merge, deploy, or trigger Render.

The BenchmarkRun supplied in this prompt is the evidence you should use to diagnose the problem.

Implement the best justified fix and validate it using local/unit/mocked tests only.

After those tests, STOP.

Do not run a new benchmark to verify your own fix. The orchestration layer will perform the post-fix benchmark later.

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
        "CODEX_ACCESS_TOKEN", "GITHUB_TOKEN",
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
            "BUBBLE_API_TOKEN", "GITHUB_TOKEN",
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
    for name in CODEX_CHILD_STRIPPED_ENV_VARS:
        environment.pop(name, None)
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


def _verify_disposable_workspace(workspace, task_id):
    workspace_root = WORKSPACE_ROOT.resolve()
    resolved_workspace = Path(workspace).resolve()
    try:
        inside_workspace_root = (
            os.path.commonpath((str(workspace_root), str(resolved_workspace)))
            == str(workspace_root)
        )
    except ValueError:
        inside_workspace_root = False
    if not inside_workspace_root or resolved_workspace == workspace_root:
        raise CodexSubmissionError(
            "Codex execution workspace is outside the disposable workspace root."
        )
    if not (resolved_workspace / ".git").is_dir():
        raise CodexSubmissionError(
            "Codex execution workspace is not a cloned Git repository."
        )
    print(
        "[CODEX] Render sandbox unavailable; using isolated workspace "
        "execution mode.",
        flush=True,
    )
    print(f"[CODEX] Verified disposable repo workspace: {task_id}", flush=True)
    return resolved_workspace


def _verify_publish_branch(branch):
    if branch == CODEX_BASE_BRANCH or not branch.startswith("codex/benchmark-"):
        raise CodexSubmissionError("Refusing to publish an unsafe Git branch.")


@contextmanager
def _git_authenticated_environment(github_token):
    with tempfile.TemporaryDirectory(prefix="rentee-git-auth-") as temp_dir:
        askpass_path = Path(temp_dir) / "git-askpass.sh"
        askpass_path.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' \"$RENTEE_GITHUB_USERNAME\" ;;\n"
            "  *) printf '%s\\n' \"$RENTEE_GITHUB_PASSWORD\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass_path.chmod(0o700)
        environment = os.environ.copy()
        for name in (
            "OPENAI_API_KEY", "BENCHMARK_API_KEY", "BUBBLE_API_TOKEN",
            "CODEX_ACCESS_TOKEN", "GITHUB_TOKEN",
        ):
            environment.pop(name, None)
        environment.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(askpass_path),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "RENTEE_GITHUB_USERNAME": "x-access-token",
            "RENTEE_GITHUB_PASSWORD": github_token,
        })
        print(
            "[CODEX] GitHub authentication configured for orchestration.",
            flush=True,
        )
        yield environment


def _github_headers(github_token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_error(action, response, github_token):
    body = _sanitize_cli_output(response.text, (github_token,))
    return CodexSubmissionError(
        f"GitHub {action} failed: HTTP {response.status_code}: {body}"
    )


def _pull_request_body(
    run_id, environment, task_id, base_commit, fix_commit,
    changed_files, final_summary,
):
    files = "\n".join(f"- {path}" for path in changed_files)
    summary = _sanitize_cli_output(final_summary)
    return f"""Automated Rentee benchmark fix

Benchmark Run:
{run_id}

Environment:
{environment.upper()}

Human reviewed:
Yes

Codex task:
{task_id}

Base commit:
{base_commit}

Fix commit:
{fix_commit}

Changed files:
{files}

Codex summary:
{summary or 'No final summary was returned.'}

This PR was generated automatically from a human-reviewed Rentee benchmark failure.

Auto-merge is NOT enabled.
"""


def _find_or_create_pull_request(
    github_token, branch, run_id, body
):
    endpoint = f"{GITHUB_API_URL}/repos/{CODEX_REPOSITORY}/pulls"
    headers = _github_headers(github_token)
    existing = requests.get(
        endpoint,
        headers=headers,
        params={
            "state": "open", "head": f"chaz1505:{branch}",
            "base": CODEX_BASE_BRANCH,
        },
        timeout=GITHUB_REQUEST_TIMEOUT_SECONDS,
    )
    if not existing.ok:
        raise _github_error("pull request lookup", existing, github_token)
    existing_pulls = existing.json()
    if existing_pulls:
        pull = existing_pulls[0]
        print(f"[CODEX] Reusing existing GitHub PR #{pull['number']}.", flush=True)
        return pull
    created = requests.post(
        endpoint,
        headers=headers,
        json={
            "title": f"Codex benchmark fix: {run_id}",
            "head": branch,
            "base": CODEX_BASE_BRANCH,
            "body": body,
        },
        timeout=GITHUB_REQUEST_TIMEOUT_SECONDS,
    )
    if not created.ok:
        raise _github_error("pull request creation", created, github_token)
    return created.json()


def persist_codex_changes_to_github(
    git_path, workspace, branch, run_id, environment, task_id,
    base_commit, changed_files, final_summary, progress_callback=None,
):
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise CodexSubmissionError(
            "GITHUB_TOKEN is required to persist Codex changes."
        )
    resolved_workspace = _verify_disposable_workspace(workspace, task_id)
    _verify_publish_branch(branch)
    commit_message = (
        f"Codex fix for benchmark {_safe_identifier(run_id)}"
    )
    add = _run([git_path, "add", "-A"], cwd=resolved_workspace)
    _require_success(add, "Git staging", (github_token,))
    staged = _run(
        [git_path, "diff", "--cached", "--quiet"],
        cwd=resolved_workspace,
    )
    if staged.returncode not in (0, 1):
        _require_success(staged, "Git staged-change lookup", (github_token,))
    if staged.returncode == 1:
        commit = _run([
            git_path,
            "-c", f"user.name={GIT_AUTOMATION_NAME}",
            "-c", f"user.email={GIT_AUTOMATION_EMAIL}",
            "commit", "-m", commit_message,
        ], cwd=resolved_workspace)
        _require_success(commit, "Git commit", (github_token,))
    revision = _run([git_path, "rev-parse", "HEAD"], cwd=resolved_workspace)
    _require_success(revision, "Fix commit lookup", (github_token,))
    fix_commit = revision.stdout.strip()
    if not fix_commit:
        raise CodexSubmissionError("Fix commit lookup returned no commit.")
    if progress_callback:
        progress_callback("pushing", {
            "branch": branch,
            "fix_commit": fix_commit,
        })

    _verify_publish_branch(branch)
    with _git_authenticated_environment(github_token) as git_environment:
        print("[CODEX] Checking remote branch...", flush=True)
        remote = _run(
            [git_path, "ls-remote", "--heads", "origin", branch],
            cwd=resolved_workspace,
            env=git_environment,
        )
        _require_success(remote, "Remote branch lookup", (github_token,))
        remote_commit = (remote.stdout.strip().split() or [None])[0]
        if remote_commit == fix_commit:
            print(
                "[CODEX] Remote branch already has the expected commit.",
                flush=True,
            )
        else:
            print("[CODEX] Pushing branch...", flush=True)
            push = _run(
                [git_path, "push", "origin", branch],
                cwd=resolved_workspace,
                env=git_environment,
            )
            _require_success(push, "Git branch push", (github_token,))
    body = _pull_request_body(
        run_id, environment, task_id, base_commit, fix_commit,
        changed_files, final_summary,
    )
    pull = _find_or_create_pull_request(
        github_token, branch, run_id, body
    )
    return {
        "fix_commit": fix_commit,
        "pr_number": pull["number"],
        "pr_url": pull["html_url"],
    }


def submit_codex_fix(
    prompt, run_id, benchmark_run_id, environment, task_id=None,
    progress_callback=None,
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
        execution_workspace = _verify_disposable_workspace(workspace, task_id)
        print("[CODEX] Starting local codex exec...", flush=True)
        execution = _run(
            [
                codex_path, "exec", "--ignore-user-config", "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox", "-",
            ],
            cwd=execution_workspace,
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
    metadata = {
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
    if not changed_files:
        return metadata

    if progress_callback:
        progress_callback("codex_completed", {
            "branch": branch,
        })
    published = persist_codex_changes_to_github(
        git_path, workspace, branch, run_id, environment, task_id,
        base_commit, changed_files, safe_final_output,
        progress_callback=progress_callback,
    )
    metadata.update(published)
    metadata["status"] = "pr_created"
    return metadata
