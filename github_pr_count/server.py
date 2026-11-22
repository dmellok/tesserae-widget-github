"""github_pr_count, total-open-PRs hero number."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 300
USERNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


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


def _days_open(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((datetime.now(UTC) - ts).total_seconds() // 86400))


def _bucket(core: Any, query: str, *, account_id: str | None) -> dict[str, Any]:
    url = (
        "https://api.github.com/search/issues?"
        + urllib.parse.urlencode({"q": query, "sort": "created", "order": "asc", "per_page": 1})
    )
    payload = core.request_json(url, account_id=account_id)
    items = payload.get("items") or []
    oldest = _days_open(items[0].get("created_at")) if items else 0
    return {
        "count": int(payload.get("total_count") or 0),
        "oldest_days": oldest,
    }


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
    acct_login = str(account.get("username") or "").strip().lstrip("@")
    explicit_user = (options.get("user") or "").strip().lstrip("@")

    user = explicit_user or acct_login or core.get_username(account_id)
    if not user:
        return {
            "error": (
                f"No GitHub username resolved for '{account_name}'. Enter a username in widget "
                "options or click Test on the account in GitHub Core (/plugins/github_core/)."
            )
        }
    if not USERNAME_RE.match(user):
        return {"error": f"'{user}' isn't a valid GitHub username."}

    org_mode = is_org_account and not explicit_user
    stale_days = max(1, int(options.get("stale_days") or 7))

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    mode_tag = "org" if org_mode else (f"org_{acct_login}" if is_org_account else "user")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{mode_tag}_{user}")
    cache = data_dir / f"pr_count_{safe_key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            cached["stale_days"] = stale_days
            return cached
        except (json.JSONDecodeError, OSError):
            pass

    try:
        if org_mode:
            yours = _bucket(core, f"is:pr is:open -is:draft org:{user}", account_id=account_id)
            review = _bucket(core, f"is:pr is:open is:draft org:{user}", account_id=account_id)
        elif is_org_account and acct_login:
            yours = _bucket(
                core, f"is:pr is:open org:{acct_login} author:{explicit_user}", account_id=account_id
            )
            review = _bucket(
                core,
                f"is:pr is:open org:{acct_login} review-requested:{explicit_user}",
                account_id=account_id,
            )
        else:
            yours = _bucket(core, f"is:pr is:open author:{user}", account_id=account_id)
            review = _bucket(core, f"is:pr is:open review-requested:{user}", account_id=account_id)
    except Exception as err:
        return {"error": core.coerce_error(err, account_name=account_name, user=user)}

    result: dict[str, Any] = {
        "user": user,
        "yours": yours,
        "review": review,
        "total": yours["count"] + review["count"],
        "oldest_days": max(yours["oldest_days"], review["oldest_days"]),
        "stale_days": stale_days,
    }
    if org_mode:
        result["yours_label"] = "Ready"
        result["review_label"] = "Draft"
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
