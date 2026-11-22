"""github_ci_status, full-bleed CI status banner."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 120
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

SEVERITY = {
    "failure": 3,
    "timed_out": 3,
    "startup_failure": 3,
    "action_required": 3,
    "in_progress": 2,
    "queued": 2,
    "waiting": 2,
    "neutral": 1,
    "cancelled": 1,
    "skipped": 1,
    "success": 0,
}


def _core() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get("github_core")
    return plugin.server_module if plugin is not None else None


def choices(name: str, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    del options
    if name != "account":
        return []
    core = _core()
    if core is None:
        return [{"value": "", "label": "Install GitHub Core to configure accounts.", "note": True}]
    return list(core.account_choices(require_auth=False))


def _state(run: dict[str, Any]) -> str:
    status = (run.get("status") or "").lower()
    conclusion = (run.get("conclusion") or "").lower()
    if status in {"in_progress", "queued", "waiting"}:
        return status
    return conclusion or "neutral"


def _summarise_repo(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_workflow: dict[str, dict[str, Any]] = {}
    for r in runs:
        name = r.get("name") or ""
        if name not in by_workflow:
            by_workflow[name] = r
    states: dict[str, int] = {}
    worst = "success"
    for r in by_workflow.values():
        s = _state(r)
        states[s] = states.get(s, 0) + 1
        if SEVERITY.get(s, 0) > SEVERITY.get(worst, 0):
            worst = s
    return {
        "worst": worst,
        "states": states,
        "workflow_count": len(by_workflow),
    }


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    raw = options.get("repos") or ""
    repos = [r.strip() for r in raw.replace(",", "\n").splitlines() if REPO_RE.match(r.strip())]
    if not repos:
        return {"error": "Add one or more repos (owner/repo, one per line)."}

    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget."}

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=False)
    except Exception as err:
        return {"error": core.coerce_error(err)}
    account_name = str((account or {}).get("name") or "")
    acct_key = str((account or {}).get("id") or "public")

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]", "_", f"{acct_key}_{','.join(repos)}")[:120]
    cache = data_dir / f"ci_status_{key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    per_repo: list[dict[str, Any]] = []
    fail_repos: list[str] = []
    running_repos: list[str] = []
    pass_repos: list[str] = []
    fetch_errors: list[str] = []

    for repo in repos:
        try:
            payload = core.request_json(
                f"https://api.github.com/repos/{repo}/actions/runs?per_page=30",
                account_id=account_id,
            )
        except Exception as err:
            fetch_errors.append(core.coerce_error(err, account_name=account_name, repo=repo))
            continue
        runs = payload.get("workflow_runs") or []
        if not runs:
            continue
        summary = _summarise_repo(runs)
        worst = summary["worst"]
        per_repo.append(
            {
                "repo": repo,
                "worst": worst,
                "workflow_count": summary["workflow_count"],
            }
        )
        if SEVERITY.get(worst, 0) >= SEVERITY["failure"]:
            fail_repos.append(repo)
        elif SEVERITY.get(worst, 0) >= SEVERITY["in_progress"]:
            running_repos.append(repo)
        else:
            pass_repos.append(repo)

    if not per_repo and fetch_errors:
        return {"error": fetch_errors[0]}

    if fail_repos:
        state = "failing"
    elif running_repos:
        state = "running"
    else:
        state = "passing"

    result = {
        "state": state,
        "repos": per_repo,
        "failing": fail_repos,
        "running": running_repos,
        "passing": pass_repos,
        "total": len(per_repo),
        "errors": fetch_errors,
    }
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
