import json
import os
import re
import subprocess
from datetime import datetime, timezone


def _run_git(arguments, cwd=None):
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False
    )


def _safe_error(process):
    message = (process.stderr or process.stdout or "unknown Git error").strip()
    message = re.sub(r"https://[^/@\s]+@", "https://[redacted]@", message)
    message = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[redacted]", message)
    return message


def _commit_message(result_path):
    case_id = os.path.basename(result_path).split("_")[0]
    timestamp = None
    try:
        with open(result_path, "r", encoding="utf-8") as result_file:
            result = json.load(result_file)
        case_id = result.get("case_id") or case_id
        started_at = result.get("started_at_utc")
        if started_at:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            timestamp = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"Save benchmark results: {case_id} {timestamp}"


def persist_benchmark_results(paths):
    if os.environ.get("BENCHMARK_COMMIT_RESULTS", "").strip().lower() != "true":
        return {
            "status": "skipped",
            "message": "Skipped — BENCHMARK_COMMIT_RESULTS is not true."
        }

    try:
        inside = _run_git(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {
                "status": "warning",
                "message": f"Not inside a Git work tree: {_safe_error(inside)}"
            }
        root_result = _run_git(["rev-parse", "--show-toplevel"])
        if root_result.returncode != 0:
            return {"status": "warning", "message": _safe_error(root_result)}
        repo_root = os.path.realpath(root_result.stdout.strip())

        branch_result = _run_git(["branch", "--show-current"], cwd=repo_root)
        if branch_result.returncode != 0:
            return {"status": "warning", "message": _safe_error(branch_result)}
        branch = branch_result.stdout.strip()
        if not branch:
            return {
                "status": "warning",
                "message": "No current Git branch (detached HEAD); skipping persistence."
            }

        relative_paths = []
        for path in paths:
            absolute_path = os.path.realpath(os.path.abspath(path))
            if os.path.commonpath([repo_root, absolute_path]) != repo_root:
                return {
                    "status": "warning",
                    "message": f"Refusing to stage benchmark file outside the repository: {path}"
                }
            if not os.path.isfile(absolute_path):
                return {
                    "status": "warning",
                    "message": f"Benchmark artifact does not exist: {path}"
                }
            relative_paths.append(os.path.relpath(absolute_path, repo_root))

        stage = _run_git(["add", "--", *relative_paths], cwd=repo_root)
        if stage.returncode != 0:
            return {"status": "warning", "message": f"Git add failed: {_safe_error(stage)}"}

        staged = _run_git(
            ["diff", "--cached", "--quiet", "--", *relative_paths],
            cwd=repo_root
        )
        if staged.returncode == 0:
            return {
                "status": "skipped",
                "branch": branch,
                "message": "Benchmark result files already tracked with no changes; skipping commit."
            }
        if staged.returncode != 1:
            return {
                "status": "warning",
                "branch": branch,
                "message": f"Could not inspect staged benchmark files: {_safe_error(staged)}"
            }

        commit = _run_git(
            ["commit", "-m", _commit_message(paths[0]), "--", *relative_paths],
            cwd=repo_root
        )
        if commit.returncode != 0:
            return {
                "status": "warning",
                "branch": branch,
                "message": f"Benchmark artifacts were staged but commit failed: {_safe_error(commit)}"
            }

        upstream = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo_root
        )
        if upstream.returncode != 0:
            return {
                "status": "warning",
                "branch": branch,
                "committed": True,
                "message": f"Commit succeeded locally, but branch {branch} has no configured upstream; push skipped."
            }

        push = _run_git(["push"], cwd=repo_root)
        if push.returncode != 0:
            return {
                "status": "warning",
                "branch": branch,
                "committed": True,
                "message": f"Commit succeeded locally but push failed: {_safe_error(push)}"
            }
        return {
            "status": "pushed",
            "branch": branch,
            "committed": True,
            "pushed": True,
            "paths": relative_paths,
            "message": "Committed and pushed benchmark artifacts."
        }
    except Exception as error:
        return {
            "status": "warning",
            "message": f"Unexpected Git persistence failure: {error}"
        }
