"""github_star_count, hero total-stars widget."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 600
HISTORY_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")

_QUERY = """
query($login: String!) {
  repositoryOwner(login: $login) {
    repositories(
      first: 100,
      ownerAffiliations: OWNER,
      privacy: PUBLIC,
      isFork: false,
      orderBy: { field: STARGAZERS, direction: DESC }
    ) {
      totalCount
      nodes { name stargazerCount url }
    }
  }
}
"""


def _fetch_installation_stars(
    core: Any, *, owner: str, account_id: str | None
) -> tuple[int, list[dict[str, Any]]]:
    """Sum stars across non-fork repos owned by ``owner`` via ``/installation/repositories``."""
    nodes: list[dict[str, Any]] = []
    owner_lc = owner.lower()
    for page in range(1, 11):
        payload = core.request_json(
            f"https://api.github.com/installation/repositories?per_page=100&page={page}",
            account_id=account_id,
        )
        batch = (payload or {}).get("repositories") if isinstance(payload, dict) else []
        if not isinstance(batch, list) or not batch:
            break
        for repo in batch:
            if not isinstance(repo, dict) or repo.get("fork"):
                continue
            repo_owner = str(((repo.get("owner") or {}).get("login")) or "").lower()
            if owner_lc and repo_owner and repo_owner != owner_lc:
                continue
            nodes.append(
                {
                    "name": str(repo.get("name") or ""),
                    "stargazerCount": int(repo.get("stargazers_count") or 0),
                    "url": str(repo.get("html_url") or ""),
                }
            )
        if len(batch) < 100:
            break
    return len(nodes), nodes


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


def _resolve_user(options: dict[str, Any], core: Any, account_id: str | None) -> str:
    user = (options.get("user") or "").strip().lstrip("@") or core.get_username(account_id)
    return user.strip()


def _read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [entry for entry in raw if isinstance(entry, dict)]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _append_snapshot(history: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    out = [e for e in history if e.get("date") != today]
    out.append({"date": today, "total": int(total)})
    out.sort(key=lambda e: e.get("date", ""))
    if len(out) > HISTORY_DAYS:
        out = out[-HISTORY_DAYS:]
    return out


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
    acct_login = str(account.get("username") or "").strip().lstrip("@")

    user = _resolve_user(options, core, account_id)
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
    cache = data_dir / f"star_count_{safe_key}.json"
    history_path = data_dir / f"star_history_{safe_key}.json"

    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if (
        account.get("auth_type") == "github_app_installation"
        and acct_login
        and user.lower() == acct_login.lower()
    ):
        try:
            total_repos, nodes = _fetch_installation_stars(
                core, owner=user, account_id=account_id
            )
        except Exception as err:
            return {"error": core.coerce_error(err, account_name=account_name, user=user)}
    else:
        try:
            payload = core.request_graphql(_QUERY, {"login": user}, account_id=account_id)
        except Exception as err:
            return {"error": core.coerce_error(err, account_name=account_name, user=user)}

        gql_err = core.coerce_graphql_error(
            payload, account_name=account_name, auth_type=str(account.get("auth_type") or "")
        )
        if gql_err:
            return {"error": gql_err}

        owner_node = (payload or {}).get("data", {}).get("repositoryOwner")
        if not owner_node:
            return {"error": f"GitHub account '@{user}' was not found."}

        repos_block = owner_node.get("repositories") or {}
        nodes = repos_block.get("nodes") or []
        total_repos = int(repos_block.get("totalCount") or 0)
    total_stars = sum(int(n.get("stargazerCount") or 0) for n in nodes)

    top = sorted(nodes, key=lambda n: int(n.get("stargazerCount") or 0), reverse=True)[:3]
    top_clean = [
        {
            "name": str(t.get("name") or ""),
            "stars": int(t.get("stargazerCount") or 0),
            "url": str(t.get("url") or ""),
        }
        for t in top
        if t.get("name")
    ]

    history = _append_snapshot(_read_history(history_path), total_stars)
    history_path.write_text(json.dumps(history), encoding="utf-8")

    result: dict[str, Any] = {
        "user": user,
        "total_stars": total_stars,
        "total_repos": total_repos,
        "top": top_clean,
        "history": history,
    }
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
