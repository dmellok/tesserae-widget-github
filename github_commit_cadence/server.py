"""github_commit_cadence, daily commits across the last 30 days."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 1800
WINDOW_DAYS = 30
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


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


def _flatten(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for week in weeks or []:
        ts = week.get("week")
        days = week.get("days") or []
        if not isinstance(ts, int) or len(days) != 7:
            continue
        sunday = datetime.fromtimestamp(ts, tz=UTC).date()
        for offset, count in enumerate(days):
            out.append(
                {
                    "date": (sunday + timedelta(days=offset)).isoformat(),
                    "count": int(count or 0),
                }
            )
    out.sort(key=lambda d: d["date"])
    return out


def _tail(days: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if not days:
        return []
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=n - 1)
    cutoff_key = cutoff.isoformat()
    out = [d for d in days if d["date"] >= cutoff_key]
    have = {d["date"] for d in out}
    cursor: date = cutoff
    while cursor <= today:
        key = cursor.isoformat()
        if key not in have:
            out.append({"date": key, "count": 0})
        cursor += timedelta(days=1)
    out.sort(key=lambda d: d["date"])
    return out[-n:]


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    repo = (options.get("repo") or "").strip()
    if not REPO_RE.match(repo):
        return {"error": "Set repo as 'owner/repo' (e.g. dmellok/tesserae)."}

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
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{repo.replace('/', '__')}")
    cache = data_dir / f"commit_cadence_{safe}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("bars"):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    pending = False
    try:
        weeks = core.request_json(
            f"https://api.github.com/repos/{repo}/stats/commit_activity",
            account_id=account_id,
        )
    except core.GithubAcceptedError:
        weeks = []
        pending = True
    except Exception as err:
        return {"error": core.coerce_error(err, account_name=account_name, repo=repo)}

    days = _flatten(weeks if isinstance(weeks, list) else [])
    bars = _tail(days, WINDOW_DAYS)
    last7 = sum(d["count"] for d in bars[-7:])
    last30 = sum(d["count"] for d in bars)
    weekly_avg = round(last30 / 4.3, 1) if last30 else 0.0
    busiest = max(bars, key=lambda d: d["count"], default={"date": "", "count": 0})

    result = {
        "repo": repo,
        "bars": bars,
        "last7": last7,
        "last30": last30,
        "weekly_avg": weekly_avg,
        "busiest": busiest,
        "pending": pending,
    }
    if not pending and bars:
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
