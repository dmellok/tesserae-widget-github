"""github_pr_queue, your open PRs + PRs awaiting your review."""

from __future__ import annotations

import contextlib
import json
import re
import time
import urllib.parse
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
    return list(core.account_choices(require_auth=True))


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    repo_url = item.get("repository_url") or ""
    repo = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
    return {
        "title": item.get("title") or "",
        "number": item.get("number"),
        "repo": repo,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "user": (item.get("user") or {}).get("login") or "",
        "comments": item.get("comments") or 0,
        "draft": bool(item.get("draft")),
    }


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {
            "error": "Install and enable GitHub Core to use this widget.",
            "yours": [],
            "review": [],
        }

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=True)
    except Exception as err:
        return {"error": core.coerce_error(err), "yours": [], "review": []}
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
            ),
            "yours": [],
            "review": [],
        }

    org_mode = is_org_account and not explicit_user
    max_results = max(1, int(options.get("max_results") or 6))
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    mode_tag = "org" if org_mode else (f"org_{acct_login}" if is_org_account else "user")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{acct_key}_{mode_tag}_{user}_{max_results}")
    cache = data_dir / f"pr_{safe_key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    def q(query: str) -> list[dict[str, Any]]:
        url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
            {"q": query, "per_page": str(max_results)}
        )
        payload = core.request_json(url, account_id=account_id)
        return [_slim(it) for it in (payload.get("items") or [])][:max_results]

    try:
        if org_mode:
            yours = q(f"is:pr is:open -is:draft org:{user}")
            review = q(f"is:pr is:open is:draft org:{user}")
        elif is_org_account and acct_login:
            yours = q(f"is:pr is:open org:{acct_login} author:{explicit_user}")
            review = q(f"is:pr is:open org:{acct_login} review-requested:{explicit_user}")
        else:
            yours = q(f"is:pr is:open author:{user}")
            review = q(f"is:pr is:open review-requested:{user}")
    except Exception as err:
        return {
            "error": core.coerce_error(err, account_name=account_name, user=user),
            "yours": [],
            "review": [],
        }

    result: dict[str, Any] = {"user": user, "yours": yours, "review": review}
    if org_mode:
        result["yours_label"] = "Ready"
        result["review_label"] = "Draft"
        result["yours_meta"] = "READY"
        result["review_meta"] = "DRAFT"
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
