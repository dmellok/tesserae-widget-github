"""github_inbox, every open issue, PR and discussion across the repos you own.

Issues and pull requests come from the REST search API (one query, split on
the ``pull_request`` key, with automatic 422 split fallback for Fine-grained
PATs that require an explicit ``is:issue`` or ``is:pr`` qualifier). Discussions
come from the GraphQL search API, which needs a connected account, so they are
skipped with a note when unauthenticated.
Excluded repos are filtered here rather than in the query: GitHub caps a
search query at 256 characters, which a dozen ``-repo:`` terms would blow.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 300
PAGE_SIZE = 100
MAX_PAGES = 3
CHOICES_PAGES = 5

DISCUSSIONS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: DISCUSSION, first: 100, after: $after) {
    discussionCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Discussion {
        title number url createdAt updatedAt isAnswered
        category { name isAnswerable }
        author { login }
        comments { totalCount }
        labels(first: 5) { nodes { name } }
        repository { nameWithOwner }
      }
    }
  }
}
"""


def _core() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get("github_core")
    return plugin.server_module if plugin is not None else None


# -- option parsing -----------------------------------------------------


def _split(raw: Any) -> list[str]:
    """Accept a list, or a comma / whitespace / newline separated string."""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [tok for tok in re.split(r"[\s,]+", str(raw or "").strip()) if tok]


def _owners(user: str, extra: Any) -> list[str]:
    out: list[str] = []
    for name in [user, *_split(extra)]:
        name = name.strip().lstrip("@").lower()
        if name and name not in out:
            out.append(name)
    return out


def _excluded(repo: str, exact: set[str], patterns: list[str]) -> bool:
    """``repo`` is ``owner/name``. Exact matches and patterns both apply to
    the full name and to the bare name, case-insensitively, so a user can
    type ``tesserae-widget-*`` without the owner prefix."""
    full = repo.lower()
    short = full.rsplit("/", 1)[-1]
    if full in exact or short in exact:
        return True
    return any(fnmatch.fnmatchcase(full, p) or fnmatch.fnmatchcase(short, p) for p in patterns)


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


# -- item shaping -------------------------------------------------------


def _slim_issue(item: dict[str, Any]) -> dict[str, Any]:
    repo_url = item.get("repository_url") or ""
    repo = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
    is_pr = item.get("pull_request") is not None
    return {
        "kind": "pr" if is_pr else "issue",
        "title": item.get("title") or "",
        "number": item.get("number"),
        "repo": repo,
        "url": item.get("html_url") or "",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "author": (item.get("user") or {}).get("login") or "",
        "comments": int(item.get("comments") or 0),
        "draft": bool(item.get("draft")) if is_pr else False,
        "labels": [
            str(lb.get("name"))
            for lb in (item.get("labels") or [])
            if isinstance(lb, dict) and lb.get("name")
        ][:5],
    }


def _slim_discussion(node: dict[str, Any]) -> dict[str, Any]:
    category = node.get("category") or {}
    labels = ((node.get("labels") or {}).get("nodes")) or []
    return {
        "kind": "discussion",
        "title": node.get("title") or "",
        "number": node.get("number"),
        "repo": (node.get("repository") or {}).get("nameWithOwner") or "",
        "url": node.get("url") or "",
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "author": (node.get("author") or {}).get("login") or "",
        "comments": int((node.get("comments") or {}).get("totalCount") or 0),
        "draft": False,
        "category": category.get("name") or "",
        "answerable": bool(category.get("isAnswerable")),
        "answered": bool(node.get("isAnswered")),
        "labels": [str(lb.get("name")) for lb in labels if isinstance(lb, dict) and lb.get("name")][
            :5
        ],
    }


def _sort_items(items: list[dict[str, Any]], order: str) -> None:
    def created(it: dict[str, Any]) -> str:
        return str(it.get("created_at") or "")

    def updated(it: dict[str, Any]) -> str:
        return str(it.get("updated_at") or "")

    if order == "oldest":
        items.sort(key=created)
    elif order == "newest":
        items.sort(key=created, reverse=True)
    else:
        items.sort(key=updated, reverse=True)


# -- GitHub calls -------------------------------------------------------


def _core_request_json(core: Any, url: str, account_id: str | None) -> Any:
    if account_id is not None:
        return core.request_json(url, account_id=account_id)
    return core.request_json(url)


def _core_request_graphql(
    core: Any, query: str, variables: dict[str, Any], account_id: str | None
) -> Any:
    if account_id is not None:
        return core.request_graphql(query, variables, account_id=account_id)
    return core.request_graphql(query, variables)


def _core_get_token(core: Any, account_id: str | None) -> str:
    if account_id is not None:
        return str(core.get_token(account_id) or "")
    return str(core.get_token() or "")


def _core_get_username(core: Any, account_id: str | None) -> str:
    if account_id is not None:
        return str(core.get_username(account_id) or "")
    return str(core.get_username() or "")


def _core_coerce_error(
    core: Any, err: Exception, *, account_name: str = "", user: str = ""
) -> str:
    try:
        return str(core.coerce_error(err, account_name=account_name, user=user))
    except TypeError:
        return str(core.coerce_error(err))


def _search_issues(
    core: Any, query: str, *, account_id: str | None = None
) -> tuple[list[dict[str, Any]], int, bool]:
    """Returns (items, total_count, truncated). Sorted by last update so
    the pages we do fetch are the ones with recent activity."""
    items: list[dict[str, Any]] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": str(PAGE_SIZE),
                "page": str(page),
            }
        )
        payload = _core_request_json(core, url, account_id)
        total = int(payload.get("total_count") or 0)
        batch = payload.get("items") or []
        items.extend(_slim_issue(it) for it in batch)
        if len(batch) < PAGE_SIZE or len(items) >= total:
            return items, total, False
    return items, total, len(items) < total


def _search_discussions(
    core: Any,
    query: str,
    *,
    account_id: str | None = None,
    account_name: str = "",
    auth_type: str = "",
) -> tuple[list[dict[str, Any]], int, bool]:
    items: list[dict[str, Any]] = []
    total = 0
    after: str | None = None
    for _ in range(MAX_PAGES):
        payload = _core_request_graphql(core, DISCUSSIONS_QUERY, {"q": query, "after": after}, account_id)
        if hasattr(core, "coerce_graphql_error"):
            gql_err = core.coerce_graphql_error(
                payload, account_name=account_name, auth_type=auth_type
            )
            if gql_err:
                raise RuntimeError(gql_err)
        elif payload.get("errors"):
            msg = (payload["errors"][0] or {}).get("message") or "GraphQL error"
            raise RuntimeError(f"GitHub GraphQL: {msg}")
        search = (payload.get("data") or {}).get("search") or {}
        total = int(search.get("discussionCount") or 0)
        items.extend(_slim_discussion(n) for n in (search.get("nodes") or []) if n)
        info = search.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return items, total, False
        after = info.get("endCursor")
    return items, total, len(items) < total


def choices(name: str, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Dynamic option choices: ``account`` picker + ``repo`` exclusion picker."""
    opts = options or {}
    core = _core()
    if core is None:
        return [{"value": "", "label": "Install GitHub Core to configure accounts.", "note": True}]

    if name == "account":
        if hasattr(core, "account_choices"):
            return list(core.account_choices(require_auth=False))
        return []

    if name != "repo":
        return []

    account_id = str(opts.get("account") or "").strip() or None
    user = str(opts.get("user") or "").strip().lstrip("@") or _core_get_username(core, account_id)

    if hasattr(core, "list_accessible_repos"):
        try:
            repos = core.list_accessible_repos(
                account_id=account_id, user=user, max_pages=CHOICES_PAGES
            )
            return [{"value": full, "label": full} for full in repos]
        except Exception:
            return []

    if not user:
        return []
    out: list[dict[str, Any]] = []
    for page in range(1, CHOICES_PAGES + 1):
        url = (
            f"https://api.github.com/users/{urllib.parse.quote(user)}/repos?"
            + urllib.parse.urlencode(
                {"type": "owner", "sort": "pushed", "per_page": str(PAGE_SIZE), "page": str(page)}
            )
        )
        batch = _core_request_json(core, url, account_id)
        if not isinstance(batch, list):
            break
        for repo in batch:
            if not isinstance(repo, dict) or repo.get("archived"):
                continue
            full = str(repo.get("full_name") or "")
            if full:
                out.append({"value": full, "label": full})
        if len(batch) < PAGE_SIZE:
            break
    return out


# -- fetch --------------------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget.", "items": []}

    account_id = (options.get("account") or "").strip() or None
    account: dict[str, Any] | None = None
    if hasattr(core, "resolve_account"):
        try:
            account = core.resolve_account(account_id, require_auth=False)
        except Exception as err:
            return {"error": _core_coerce_error(core, err), "items": []}
    account_name = str((account or {}).get("name") or "")
    auth_type = str((account or {}).get("auth_type") or "")
    acct_key = str((account or {}).get("id") or (account_id or "public"))

    user = (options.get("user") or "").strip().lstrip("@") or _core_get_username(core, account_id)
    if not user:
        return {
            "error": (
                "No GitHub username or org set. Pick an account in widget options, enter a "
                "username under More options, or connect a GitHub App in GitHub Core (/plugins/github_core/)."
            ),
            "items": [],
        }

    owners = _owners(user, options.get("extra_owners"))
    want_issues = _truthy(options.get("show_issues"))
    want_prs = _truthy(options.get("show_prs"))
    want_discussions = _truthy(options.get("show_discussions"))
    exact = {s.lower() for s in _split(options.get("exclude_repos"))}
    patterns = [s.lower() for s in _split(options.get("exclude_patterns"))]
    group_by = str(options.get("group_by") or "repo")
    density = str(options.get("density") or "auto")
    order = str(options.get("sort") or "updated")
    show_labels = _truthy(options.get("show_labels"))
    try:
        max_items = max(1, int(options.get("max_items") or 12))
    except (TypeError, ValueError):
        max_items = 12

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    key_src = json.dumps(
        [
            acct_key,
            owners,
            want_issues,
            want_prs,
            want_discussions,
            sorted(exact),
            patterns,
            order,
            max_items,
        ],
        sort_keys=True,
    )
    cache = data_dir / f"inbox_{hashlib.sha1(key_src.encode()).hexdigest()[:16]}.json"
    if (
        not ctx.get("fresh")
        and cache.exists()
        and time.time() - cache.stat().st_mtime < CACHE_TTL_S
    ):
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            cached["group_by"] = group_by
            cached["density"] = density
            cached["show_labels"] = show_labels
            return cached
        except (json.JSONDecodeError, OSError):
            pass

    scope = " ".join(f"user:{o}" for o in owners)
    items: list[dict[str, Any]] = []
    truncated = False
    notes: list[str] = []
    error = ""

    if want_issues or want_prs:
        base_query = f"is:open archived:false {scope}"
        query = base_query
        if want_issues != want_prs:
            query += " is:issue" if want_issues else " is:pr"
        try:
            found, _total, more = _search_issues(core, query, account_id=account_id)
            items.extend(found)
            truncated = truncated or more
        except urllib.error.HTTPError as err:
            # Fine-grained PATs require explicit is:issue or is:pr on /search/issues (422 otherwise).
            if err.code == 422 and want_issues and want_prs:
                with contextlib.suppress(Exception):
                    err.close()
                try:
                    found_i, _ti, more_i = _search_issues(
                        core, f"{base_query} is:issue", account_id=account_id
                    )
                    found_p, _tp, more_p = _search_issues(
                        core, f"{base_query} is:pr", account_id=account_id
                    )
                    items.extend(found_i)
                    items.extend(found_p)
                    truncated = truncated or more_i or more_p
                except Exception as split_err:
                    error = _core_coerce_error(
                        core, split_err, account_name=account_name, user=user
                    )
            else:
                error = _core_coerce_error(core, err, account_name=account_name, user=user)
        except Exception as err:
            error = _core_coerce_error(core, err, account_name=account_name, user=user)

    if want_discussions:
        if not _core_get_token(core, account_id):
            notes.append("Discussions need a connected GitHub account (or token) in GitHub Core.")
        else:
            try:
                found, _total, more = _search_discussions(
                    core,
                    f"is:open {scope}",
                    account_id=account_id,
                    account_name=account_name,
                    auth_type=auth_type,
                )
                items.extend(found)
                truncated = truncated or more
            except Exception as err:
                notes.append(
                    f"Discussions unavailable: {_core_coerce_error(core, err, account_name=account_name, user=user)}"
                )

    if error and not items:
        return {"error": error, "items": [], "user": user}

    items = [it for it in items if not _excluded(it["repo"], exact, patterns)]
    counts = {"issue": 0, "pr": 0, "discussion": 0}
    by_repo: dict[str, dict[str, int]] = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
        repo_counts = by_repo.setdefault(it["repo"], {"issue": 0, "pr": 0, "discussion": 0})
        repo_counts[it["kind"]] = repo_counts.get(it["kind"], 0) + 1
    _sort_items(items, order)
    total = len(items)
    items = items[:max_items]

    result: dict[str, Any] = {
        "user": user,
        "owners": owners,
        "items": items,
        "counts": counts,
        "by_repo": by_repo,
        "total": total,
        "shown": len(items),
        "truncated": truncated,
        "sort": order,
        "group_by": group_by,
        "density": density,
        "show_labels": show_labels,
        "note": " ".join(notes),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error:
        result["note"] = (result["note"] + " " + error).strip()
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
