"""github_inbox, every open issue, PR and discussion across the repos you own.

Issues and pull requests come from the REST search API (one query, split on
the ``pull_request`` key). Discussions come from the GraphQL search API,
which needs a token, so they are skipped with a note when none is set.
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
    return current_app.config["PLUGIN_REGISTRY"].get("github_core").server_module


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


def _search_issues(core: Any, query: str) -> tuple[list[dict[str, Any]], int, bool]:
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
        payload = core.request_json(url)
        total = int(payload.get("total_count") or 0)
        batch = payload.get("items") or []
        items.extend(_slim_issue(it) for it in batch)
        if len(batch) < PAGE_SIZE or len(items) >= total:
            return items, total, False
    return items, total, len(items) < total


def _search_discussions(core: Any, query: str) -> tuple[list[dict[str, Any]], int, bool]:
    items: list[dict[str, Any]] = []
    total = 0
    after: str | None = None
    for _ in range(MAX_PAGES):
        payload = core.request_graphql(DISCUSSIONS_QUERY, {"q": query, "after": after})
        if payload.get("errors"):
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


def choices(name: str) -> list[dict[str, str]]:
    """Repo picker for the editor: the repos owned by the GitHub Core
    username, newest push first, archived ones left out."""
    if name != "repo":
        return []
    core = _core()
    user = core.get_username()
    if not user:
        return []
    out: list[dict[str, str]] = []
    for page in range(1, CHOICES_PAGES + 1):
        url = (
            f"https://api.github.com/users/{urllib.parse.quote(user)}/repos?"
            + urllib.parse.urlencode(
                {"type": "owner", "sort": "pushed", "per_page": str(PAGE_SIZE), "page": str(page)}
            )
        )
        batch = core.request_json(url)
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
    user = (options.get("user") or "").strip() or core.get_username()
    if not user:
        return {"error": "Set a GitHub username, here or as the GitHub Core default.", "items": []}

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
        query = f"is:open archived:false {scope}"
        if want_issues != want_prs:
            query += " is:issue" if want_issues else " is:pr"
        try:
            found, _total, more = _search_issues(core, query)
            items.extend(found)
            truncated = truncated or more
        except Exception as err:
            error = core.coerce_error(err)

    if want_discussions:
        if not core.get_token():
            notes.append("Discussions need a token in GitHub Core.")
        else:
            try:
                found, _total, more = _search_discussions(core, f"is:open {scope}")
                items.extend(found)
                truncated = truncated or more
            except Exception as err:
                notes.append(f"Discussions unavailable: {core.coerce_error(err)}")

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
