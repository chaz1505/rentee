import base64
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import requests


GITHUB_REPOSITORY = "chaz1505/rentee"
GITHUB_BRANCH = "main"
GITHUB_API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT = 30


def _safe_message(value, secrets):
    message = str(value or "").strip()
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[redacted]", message)
    return message


def _repo_relative_result_path(path):
    normalized = os.path.abspath(path).replace(os.sep, "/")
    marker = "/tests/results/"
    if marker not in normalized:
        raise ValueError(f"Artifact is not inside tests/results: {path}")
    filename = normalized.rsplit(marker, 1)[1]
    if not filename or "/" in filename or filename == ".autotest_state.json":
        raise ValueError(f"Invalid benchmark artifact path: {path}")
    return f"tests/results/{filename}"


def _artifact_metadata(paths):
    raw_path = next(
        (
            path for path in paths
            if path.endswith(".json") and not path.endswith("_evaluation.json")
        ),
        paths[0]
    )
    case_id = os.path.basename(raw_path).split("_")[0]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        with open(raw_path, "r", encoding="utf-8") as source:
            result = json.load(source)
        case_id = result.get("case_id") or case_id
        started_at = result.get("started_at_utc")
        if started_at:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            timestamp = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return case_id, timestamp


def _commit_message(path, case_id, timestamp):
    if path.endswith("_evaluation.json"):
        kind = "evaluation"
    elif path.endswith("_fix_prompt.md"):
        kind = "fix prompt"
    else:
        kind = "result"
    return f"Save benchmark {kind}: {case_id} {timestamp}"


def publish_benchmark_results(paths):
    if os.environ.get("BENCHMARK_SKIP_GITHUB", "").strip().lower() == "true":
        return {
            "status": "skipped",
            "message": "Skipped — BENCHMARK_SKIP_GITHUB is true."
        }

    token = os.environ.get("GITHUB_RESULTS_TOKEN", "")
    if not token:
        return {
            "status": "skipped",
            "message": (
                "GITHUB_RESULTS_TOKEN is not configured; benchmark artifacts remain local."
            )
        }

    secrets = [
        token,
        os.environ.get("BUBBLE_API_TOKEN", ""),
        os.environ.get("OPENAI_API_KEY", "")
    ]
    try:
        if len(paths) != 3:
            raise ValueError("Exactly three benchmark artifact paths are required.")
        artifacts = []
        for path in paths:
            repo_path = _repo_relative_result_path(path)
            with open(path, "rb") as source:
                content = source.read()
            for secret in secrets:
                if secret and secret.encode("utf-8") in content:
                    return {
                        "status": "warning",
                        "message": (
                            f"SECURITY: Refusing to publish {repo_path} because a configured "
                            "secret value was found in the artifact. All files remain local."
                        )
                    }
            artifacts.append((path, repo_path, content))

        repo_paths = {artifact[1] for artifact in artifacts}
        raw_paths = [
            repo_path for repo_path in repo_paths
            if repo_path.endswith(".json")
            and not repo_path.endswith("_evaluation.json")
        ]
        if len(raw_paths) != 1:
            raise ValueError("The artifact set must contain one raw benchmark JSON file.")
        basename = raw_paths[0][:-5]
        expected_paths = {
            f"{basename}.json",
            f"{basename}_evaluation.json",
            f"{basename}_fix_prompt.md"
        }
        if repo_paths != expected_paths:
            raise ValueError(
                "Only the matching raw result, evaluation, and fix-prompt artifacts may "
                "be published."
            )

        case_id, timestamp = _artifact_metadata(paths)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        published = []
        for local_path, repo_path, content in artifacts:
            url = (
                f"{GITHUB_API_ROOT}/repos/{GITHUB_REPOSITORY}/contents/"
                f"{quote(repo_path, safe='/')}"
            )
            existing = requests.get(
                url,
                headers=headers,
                params={"ref": GITHUB_BRANCH},
                timeout=REQUEST_TIMEOUT
            )
            sha = None
            if existing.status_code == 200:
                existing_body = existing.json()
                sha = existing_body.get("sha")
                if not sha:
                    raise RuntimeError(f"GitHub returned no SHA for existing file {repo_path}")
            elif existing.status_code != 404:
                try:
                    detail = existing.json().get("message", existing.text)
                except ValueError:
                    detail = existing.text
                raise RuntimeError(
                    f"HTTP {existing.status_code} while checking {repo_path}: {detail}"
                )

            payload = {
                "message": _commit_message(local_path, case_id, timestamp),
                "content": base64.b64encode(content).decode("ascii"),
                "branch": GITHUB_BRANCH
            }
            if sha:
                payload["sha"] = sha
            published_response = requests.put(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )
            if published_response.status_code not in (200, 201):
                try:
                    detail = published_response.json().get(
                        "message", published_response.text
                    )
                except ValueError:
                    detail = published_response.text
                raise RuntimeError(
                    f"HTTP {published_response.status_code} publishing {repo_path}: {detail}"
                )
            published.append(repo_path)

        return {
            "status": "published",
            "published": published,
            "message": (
                f"Published {len(published)} benchmark artifacts to "
                f"{GITHUB_REPOSITORY} on {GITHUB_BRANCH}."
            )
        }
    except Exception as error:
        return {
            "status": "warning",
            "message": (
                "FAILED — benchmark artifacts remain local. Reason: "
                f"{_safe_message(error, secrets)}"
            )
        }
