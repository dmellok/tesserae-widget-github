"""github_activity_heatmap, 12-week public-events heatmap."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 600
PAGE_BUDGET = 6
PER_PAGE = 100
USERNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
WINDOW_DAYS = 84

TYPE_BUCKET = {
    "PushEvent": "commits",
    "PullRequestEvent": "prs",
    "IssuesEvent": "issues",
    "ReleaseEvent": "releases",
    "PullRequestReviewEvent": "reviews",
    "PullRequestReviewCommentEvent": "reviews",
    "IssueCommentEvent": "issues",
    "CommitCommentEvent": "commits",
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


def _level(count: int, breakpoints: list[int]) -> int:
    if count <= 0:
        return 0
    for idx, bp in enumerate(breakpoints, start=1):
        if count <= bp:
            return idx
    return 4


def _breakpoints(counts: list[int]) -> list[int]:
    nz = sorted(c for c in counts if c > 0)
    if not nz:
        return [1, 2, 3]
    n = len(nz)

    def q(p: float) -> int:
        idx = max(0, min(n - 1, round(p * (n - 1))))
        return nz[idx]

    return [q(0.33), q(0.66), q(0.95)]


def _grid(by_day: dict[str, dict[str, int]], today: datetime) -> list[list[dict[str, Any]]]:
    start = (today - timedelta(days=WINDOW_DAYS - 1)).date()
    while start.weekday() != 0:
        start -= timedelta(days=1)

    weeks: list[list[dict[str, Any]]] = []
    cursor = start
    end = today.date()
    while cursor <= end:
        week: list[dict[str, Any]] = []
        for _ in range(7):
            key = cursor.isoformat()
            day = by_day.get(key, {})
            total = sum(v for k, v in day.items() if k != "events")
            week.append(
                {
                    "date": key,
                    "count": total,
                    "by_type": dict(day),
                }
            )
            cursor += timedelta(days=1)
            if cursor > end:
                break
        weeks.append(week)

    flat_counts = [cell["count"] for w in weeks for cell in w]
    bp = _breakpoints(flat_counts)
    for w in weeks:
        for cell in w:
            cell["level"] = _level(cell["count"], bp)
    return weeks


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
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

    explicit_user = (options.get("user") or "").strip().lstrip("@")
    user = explicit_user or core.get_username(account_id)
    if not user:
        return {
            "error": (
                "No GitHub username set. Enter a username in widget options or connect an "
                "account in GitHub Core (/plugins/github_core/)."
            )
        }
    if not USERNAME_RE.match(user):
        return {"error": f"'{user}' isn't a valid GitHub username."}

    is_org_events = (
        not explicit_user and str((account or {}).get("account_type") or "").lower() == "organization"
    )
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(
        r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{'org' if is_org_events else 'user'}_{user}"
    )
    cache = data_dir / f"activity_heatmap_{safe_key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    today = datetime.now(UTC)
    cutoff = today - timedelta(days=WINDOW_DAYS)
    by_day: dict[str, dict[str, int]] = {}
    totals = {"commits": 0, "prs": 0, "issues": 0, "releases": 0, "reviews": 0}
    by_repo: dict[str, int] = {}
    fetched = 0

    for page in range(1, PAGE_BUDGET + 1):
        url = (
            f"https://api.github.com/orgs/{user}/events?per_page={PER_PAGE}&page={page}"
            if is_org_events
            else f"https://api.github.com/users/{user}/events/public?per_page={PER_PAGE}&page={page}"
        )
        try:
            events = core.request_json(url, account_id=account_id)
        except Exception as err:
            if page == 1:
                return {"error": core.coerce_error(err, account_name=account_name, user=user)}
            break
        if not isinstance(events, list) or not events:
            break
        oldest_in_page = None
        for event in events:
            ts = event.get("created_at")
            if not ts:
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < cutoff:
                continue
            oldest_in_page = when
            day_key = when.date().isoformat()
            bucket = TYPE_BUCKET.get(event.get("type") or "")
            if bucket is None:
                continue
            slot = by_day.setdefault(day_key, {})
            slot[bucket] = int(slot.get(bucket, 0)) + 1
            totals[bucket] = totals.get(bucket, 0) + 1
            fetched += 1
            repo_name = (event.get("repo") or {}).get("name")
            if repo_name:
                by_repo[repo_name] = by_repo.get(repo_name, 0) + 1
        if oldest_in_page and oldest_in_page < cutoff:
            break

    grid = _grid(by_day, today)
    busiest = max(
        (cell for w in grid for cell in w),
        key=lambda c: c["count"],
        default={"date": "", "count": 0},
    )
    top_repos = sorted(by_repo.items(), key=lambda kv: kv[1], reverse=True)[:3]

    result = {
        "user": user,
        "grid": grid,
        "totals": totals,
        "fetched": fetched,
        "busiest": busiest,
        "top_repos": [{"name": n, "count": c} for n, c in top_repos],
    }
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
