"""github_releases, latest releases across watched repos."""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

from flask import current_app

CACHE_TTL_S = 600
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


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


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = SEMVER_RE.match(tag or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _bump_type(curr: tuple[int, int, int] | None, prev: tuple[int, int, int] | None) -> str | None:
    if not curr or not prev:
        return None
    if curr[0] > prev[0]:
        return "major"
    if curr[1] > prev[1]:
        return "minor"
    if curr[2] > prev[2]:
        return "patch"
    return None


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    raw = options.get("repos") or ""
    repos = [r.strip() for r in raw.replace(",", "\n").splitlines() if REPO_RE.match(r.strip())]
    if not repos:
        return {"error": "Add one or more repos (owner/repo, one per line).", "releases": []}

    core = _core()
    if core is None:
        return {"error": "Install and enable GitHub Core to use this widget.", "releases": []}

    account_id = (options.get("account") or "").strip() or None
    try:
        account = core.resolve_account(account_id, require_auth=False)
    except Exception as err:
        return {"error": core.coerce_error(err), "releases": []}
    account_name = str((account or {}).get("name") or "")
    acct_key = str((account or {}).get("id") or "public")

    max_per = max(1, int(options.get("max_results") or 1))
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]", "_", f"{acct_key}_{','.join(repos)}_{max_per}")[:120]
    cache = data_dir / f"rel_v2_{key}.json"
    if not ctx.get("fresh") and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for repo in repos:
        try:
            rels = core.request_json(
                f"https://api.github.com/repos/{repo}/releases?per_page={max_per + 1}",
                account_id=account_id,
            )
        except Exception as err:
            errors.append(core.coerce_error(err, account_name=account_name, repo=repo))
            continue
        rels = list(rels or [])

        commits_since_latest: int | None = None
        default_branch: str | None = None
        if rels:
            try:
                repo_meta = core.request_json(
                    f"https://api.github.com/repos/{repo}",
                    account_id=account_id,
                )
                default_branch = repo_meta.get("default_branch") or "main"
            except Exception:
                default_branch = "main"
            try:
                cmp_payload = core.request_json(
                    f"https://api.github.com/repos/{repo}/compare/"
                    f"{rels[0].get('tag_name')}...{default_branch}",
                    account_id=account_id,
                )
                tot = cmp_payload.get("total_commits")
                if isinstance(tot, int):
                    commits_since_latest = tot
            except Exception:
                commits_since_latest = None

        for idx, r in enumerate(rels[:max_per]):
            tag = r.get("tag_name") or ""
            curr_ver = _parse_semver(tag)
            prev_ver = (
                _parse_semver((rels[idx + 1] or {}).get("tag_name") or "")
                if idx + 1 < len(rels)
                else None
            )
            entry = {
                "repo": repo,
                "tag": tag,
                "name": r.get("name") or tag,
                "published_at": r.get("published_at"),
                "prerelease": bool(r.get("prerelease")),
                "draft": bool(r.get("draft")),
                "bump_type": _bump_type(curr_ver, prev_ver),
            }
            if idx == 0 and commits_since_latest is not None:
                entry["commits_since"] = commits_since_latest
                entry["default_branch"] = default_branch
            out.append(entry)

    if not out and errors:
        return {"error": errors[0], "releases": []}

    out.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    result: dict[str, Any] = {"releases": out}
    if errors:
        result["errors"] = errors
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
