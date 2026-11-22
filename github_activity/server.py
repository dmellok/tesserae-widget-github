"""github_activity, recent public events for a user."""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 300


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


def _cached(path: Path) -> Any | None:
    if not path.exists() or time.time() - path.stat().st_mtime >= CACHE_TTL_S:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


EVENT_KINDS = {
    "PushEvent": ("git-commit", "pushed"),
    "PullRequestEvent": ("git-pull-request", "PR"),
    "IssuesEvent": ("warning-circle", "issue"),
    "IssueCommentEvent": ("chat-circle", "commented"),
    "PullRequestReviewEvent": ("eye", "reviewed"),
    "PullRequestReviewCommentEvent": ("chats", "review"),
    "CreateEvent": ("plus-circle", "created"),
    "DeleteEvent": ("minus-circle", "deleted"),
    "ForkEvent": ("git-fork", "forked"),
    "WatchEvent": ("star", "starred"),
    "ReleaseEvent": ("tag", "released"),
    "PublicEvent": ("globe", "made public"),
}


def _slim_event(ev: dict[str, Any]) -> dict[str, Any]:
    kind, label = EVENT_KINDS.get(ev.get("type", ""), ("activity", ev.get("type", "")))
    payload = ev.get("payload") or {}
    repo = (ev.get("repo") or {}).get("name") or ""
    detail = ""
    if ev["type"] == "PushEvent":
        commits = payload.get("commits") or []
        n = payload.get("size") or len(commits)
        detail = f"{n} commit{'' if n == 1 else 's'}"
    elif ev["type"] == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        action = payload.get("action") or ""
        detail = f"{action} #{pr.get('number', '?')}"
    elif ev["type"] == "IssuesEvent":
        issue = payload.get("issue") or {}
        action = payload.get("action") or ""
        detail = f"{action} #{issue.get('number', '?')}"
    elif ev["type"] == "ReleaseEvent":
        rel = payload.get("release") or {}
        detail = rel.get("tag_name") or ""
    elif ev["type"] == "CreateEvent":
        detail = payload.get("ref_type") or ""
    return {
        "icon": kind,
        "label": label,
        "repo": repo,
        "detail": detail,
        "at": ev.get("created_at"),
    }


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget.", "events": []}

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=False)
    except Exception as err:
        return {"error": core.coerce_error(err), "events": []}
    account_name = str((account or {}).get("name") or "")
    acct_key = str((account or {}).get("id") or "public")

    explicit_user = (options.get("user") or "").strip().lstrip("@")
    user = explicit_user or core.get_username(account_id)
    if not user:
        return {
            "error": (
                "No GitHub username set. Enter a username in widget options or connect an "
                "account in GitHub Core (/plugins/github_core/)."
            ),
            "events": [],
        }

    is_org_events = (
        not explicit_user and str((account or {}).get("account_type") or "").lower() == "organization"
    )
    max_events = int(options.get("max_events") or 10)
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(
        r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{'org' if is_org_events else 'user'}_{user}"
    )
    cache_path = data_dir / f"events_{safe_key}.json"
    if not ctx.get("fresh"):
        cached = _cached(cache_path)
        if cached is not None:
            cached["events"] = cached.get("events", [])[:max_events]
            return cached

    events_url = (
        f"https://api.github.com/orgs/{user}/events?per_page=100"
        if is_org_events
        else f"https://api.github.com/users/{user}/events/public?per_page=100"
    )
    try:
        raw = core.request_json(events_url, account_id=account_id)
    except Exception as err:
        return {
            "error": core.coerce_error(err, account_name=account_name, user=user),
            "events": [],
        }

    all_events = [_slim_event(ev) for ev in (raw or [])]

    now = datetime.now(UTC)
    seven_days = [0] * 7
    daily_typed: list[dict[str, int]] = [
        {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "other": 0} for _ in range(7)
    ]
    type_counts = {
        "PushEvent": 0,
        "PullRequestEvent": 0,
        "IssuesEvent": 0,
        "ReleaseEvent": 0,
        "other": 0,
    }
    TYPE_BUCKET = {
        "PushEvent": "commits",
        "PullRequestEvent": "prs",
        "IssuesEvent": "issues",
        "ReleaseEvent": "releases",
    }
    repos_set: set[str] = set()
    active_dates: set[Any] = set()
    for ev_raw, ev in zip((raw or []), all_events, strict=False):
        repos_set.add(ev.get("repo") or "")
        kind = ev_raw.get("type") or ""
        if kind in type_counts:
            type_counts[kind] += 1
        else:
            type_counts["other"] += 1
        at = ev.get("at")
        if not at:
            continue
        try:
            dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        active_dates.add(dt.date())
        delta_days = (now.date() - dt.date()).days
        if 0 <= delta_days < 7:
            seven_days[6 - delta_days] += 1
            bucket = TYPE_BUCKET.get(kind, "other")
            daily_typed[6 - delta_days][bucket] += 1

    streak = 0
    cursor = now.date()
    if cursor not in active_dates:
        cursor = cursor - timedelta(days=1)
    while cursor in active_dates:
        streak += 1
        cursor = cursor - timedelta(days=1)

    events = all_events[:max_events]
    result = {
        "user": user,
        "events": events,
        "count": len(events),
        "total_30": len(all_events),
        "daily": seven_days,
        "daily_typed": daily_typed,
        "streak": streak,
        "repos_count": len([r for r in repos_set if r]),
        "type_commits": type_counts.get("PushEvent", 0),
        "type_prs": type_counts.get("PullRequestEvent", 0),
        "type_issues": type_counts.get("IssuesEvent", 0),
        "type_releases": type_counts.get("ReleaseEvent", 0),
    }
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    return result
