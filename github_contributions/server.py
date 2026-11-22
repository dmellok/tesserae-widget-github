"""github_contributions, the year's contribution heatmap."""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 1800

QUERY = """
query($user: String!) {
  user(login: $user) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
            contributionLevel
          }
        }
      }
    }
  }
}
""".strip()

LEVEL_TO_INT = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
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
    return list(core.account_choices(require_auth=True))


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget.", "weeks": []}

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=True)
    except Exception as err:
        return {"error": core.coerce_error(err), "weeks": []}
    assert account is not None
    account_name = str(account.get("name") or "")
    acct_key = str(account.get("id") or "default")
    is_org_account = str(account.get("account_type") or "").lower() == "organization"
    explicit_user = (options.get("user") or "").strip().lstrip("@")

    if is_org_account and not explicit_user:
        org_login = str(account.get("username") or account_name)
        return {
            "error": (
                f"GitHub only provides contribution calendars for individual users, not organizations "
                f"(@{org_login}). Enter a personal GitHub username in this widget's Username option, "
                "or select a user account."
            ),
            "weeks": [],
        }

    user = explicit_user or core.get_username(account_id)
    if not user:
        return {
            "error": (
                f"No GitHub username resolved for '{account_name}'. Enter a username in widget "
                "options or click Test on the account in GitHub Core (/plugins/github_core/)."
            ),
            "weeks": [],
        }

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{user}")
    cache = data_dir / f"contrib_v2_{safe_key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    legacy = data_dir / f"contrib_{user}.json"
    if legacy.exists():
        with contextlib.suppress(OSError):
            legacy.unlink()

    try:
        payload = core.request_graphql(QUERY, {"user": user}, account_id=account_id)
    except Exception as err:
        return {
            "error": core.coerce_error(err, account_name=account_name, user=user),
            "weeks": [],
        }

    gql_err = core.coerce_graphql_error(
        payload, account_name=account_name, auth_type=str(account.get("auth_type") or "")
    )
    if gql_err:
        return {"error": gql_err, "weeks": []}

    cal = (
        ((payload.get("data") or {}).get("user") or {}).get("contributionsCollection") or {}
    ).get("contributionCalendar")
    if not cal:
        return {"error": f"No contribution data found for @{user} (check the username).", "weeks": []}

    weeks = []
    for w in cal.get("weeks") or []:
        days = []
        for d in w.get("contributionDays") or []:
            days.append(
                {
                    "date": d.get("date"),
                    "count": d.get("contributionCount") or 0,
                    "level": LEVEL_TO_INT.get(d.get("contributionLevel"), 0),
                }
            )
        weeks.append(days)

    flat = sorted(
        (d for w in weeks for d in w),
        key=lambda d: d.get("date") or "",
    )
    longest = 0
    run = 0
    busiest = {"date": "", "count": 0}
    for d in flat:
        c = d.get("count") or 0
        if c > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        if c > busiest["count"]:
            busiest = {"date": d.get("date") or "", "count": c}

    current_streak = 0
    for d in reversed(flat):
        if (d.get("count") or 0) > 0:
            current_streak += 1
        else:
            if d is flat[-1]:
                continue
            break

    this_week = sum((d.get("count") or 0) for d in flat[-7:])
    this_month = sum((d.get("count") or 0) for d in flat[-30:])

    result = {
        "user": user,
        "total": cal.get("totalContributions") or 0,
        "weeks": weeks,
        "current_streak": current_streak,
        "longest_streak": longest,
        "busiest_date": busiest["date"],
        "busiest_count": busiest["count"],
        "this_week": this_week,
        "this_month": this_month,
        # Installation tokens act as the app, not the user, so GitHub may leave
        # out the user's private contributions.
        "public_only": str(account.get("auth_type") or "") == "github_app_installation",
    }
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
