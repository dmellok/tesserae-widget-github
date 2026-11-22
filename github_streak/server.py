"""github_streak, current contribution streak as a single number."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 900
USERNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")

_QUERY = """
query($user: String!) {
  user(login: $user) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
""".strip()


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
    return list(core.account_choices(require_auth=True))


def _flatten(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: list[dict[str, Any]] = []
    for week in weeks or []:
        for day in week.get("contributionDays") or []:
            date = day.get("date")
            count = int(day.get("contributionCount") or 0)
            if date:
                days.append({"date": date, "count": count})
    days.sort(key=lambda d: d["date"])
    return days


def _current_streak(days: list[dict[str, Any]]) -> int:
    if not days:
        return 0
    tail = list(reversed(days))
    started = False
    streak = 0
    for entry in tail:
        if not started and entry["count"] == 0:
            continue
        started = True
        if entry["count"] > 0:
            streak += 1
        else:
            break
    return streak


def _longest_streak(days: list[dict[str, Any]]) -> int:
    best = 0
    run = 0
    for entry in days:
        if entry["count"] > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget."}

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=True)
    except Exception as err:
        return {"error": core.coerce_error(err)}
    assert account is not None
    account_name = str(account.get("name") or "")
    acct_key = str(account.get("id") or "default")
    is_org_account = str(account.get("account_type") or "").lower() == "organization"
    explicit_user = (options.get("user") or "").strip().lstrip("@")

    if is_org_account and not explicit_user:
        org_login = str(account.get("username") or account_name)
        return {
            "error": (
                f"GitHub only provides contribution streaks for individual users, not organizations "
                f"(@{org_login}). Enter a personal GitHub username in this widget's Username option, "
                "or select a user account."
            )
        }

    user = explicit_user or core.get_username(account_id)
    if not user:
        return {
            "error": (
                f"No GitHub username resolved for '{account_name}'. Enter a username in widget "
                "options or click Test on the account in GitHub Core (/plugins/github_core/)."
            )
        }
    if not USERNAME_RE.match(user):
        return {"error": f"'{user}' isn't a valid GitHub username."}

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{user}")
    cache = data_dir / f"streak_{safe_key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    try:
        payload = core.request_graphql(_QUERY, {"user": user}, account_id=account_id)
    except Exception as err:
        return {"error": core.coerce_error(err, account_name=account_name, user=user)}

    gql_err = core.coerce_graphql_error(
        payload, account_name=account_name, auth_type=str(account.get("auth_type") or "")
    )
    if gql_err:
        return {"error": gql_err}

    user_node = (payload or {}).get("data", {}).get("user")
    if not user_node:
        return {"error": f"GitHub user '@{user}' was not found."}

    cal = (user_node.get("contributionsCollection") or {}).get("contributionCalendar") or {}
    days = _flatten(cal.get("weeks") or [])
    today = days[-1] if days else None

    result = {
        "user": user,
        "current_streak": _current_streak(days),
        "longest_streak": _longest_streak(days),
        "today_count": int((today or {}).get("count") or 0),
        "year_total": int(cal.get("totalContributions") or 0),
        # Installation tokens act as the app, not the user, so GitHub may leave
        # out the user's private contributions.
        "public_only": str(account.get("auth_type") or "") == "github_app_installation",
    }
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
