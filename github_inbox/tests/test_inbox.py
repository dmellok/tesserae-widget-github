"""github_inbox: option parsing, exclusion, kind split, sort, cap, cache and
the repo picker. No network: github_core is replaced by a fake."""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("gi_server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load()


def _issue(
    repo: str, n: int, *, pr: bool = False, created: str, updated: str, **extra: Any
) -> dict:
    it = {
        "title": f"{repo} #{n}",
        "number": n,
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/{'pull' if pr else 'issues'}/{n}",
        "created_at": created,
        "updated_at": updated,
        "user": {"login": "someone"},
        "comments": 2,
        "labels": [{"name": "bug"}, {"name": "help wanted"}],
    }
    if pr:
        it["pull_request"] = {"url": "x"}
        it["draft"] = extra.get("draft", False)
    return it


ISSUES = [
    _issue("dmellok/tesserae", 1, created="2026-01-01T00:00:00Z", updated="2026-10-10T00:00:00Z"),
    _issue(
        "dmellok/tesserae",
        2,
        pr=True,
        draft=True,
        created="2026-09-01T00:00:00Z",
        updated="2026-10-12T00:00:00Z",
    ),
    _issue(
        "dmellok/tesserae-widget-sky",
        3,
        created="2026-05-01T00:00:00Z",
        updated="2026-10-11T00:00:00Z",
    ),
    _issue(
        "dmellok/tesserae-widgets",
        4,
        pr=True,
        created="2026-10-01T00:00:00Z",
        updated="2026-10-15T00:00:00Z",
    ),
]

DISCUSSION_PAYLOAD = {
    "data": {
        "search": {
            "discussionCount": 1,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "title": "Localization?",
                    "number": 134,
                    "url": "https://github.com/dmellok/tesserae/discussions/134",
                    "createdAt": "2026-07-21T12:25:08Z",
                    "updatedAt": "2026-10-14T06:37:34Z",
                    "isAnswered": False,
                    "category": {"name": "Q&A", "isAnswerable": True},
                    "author": {"login": "docbobo"},
                    "comments": {"totalCount": 8},
                    "labels": {"nodes": []},
                    "repository": {"nameWithOwner": "dmellok/tesserae"},
                }
            ],
        }
    }
}


class FakeCore:
    def __init__(self, *, token: str = "tok", username: str = "dmellok", issues=None, repos=None):
        self.token = token
        self.username = username
        self.issues = ISSUES if issues is None else issues
        self.repos = repos or []
        self.rest_calls: list[str] = []
        self.graphql_calls: list[dict] = []

    def get_token(self):
        return self.token

    def get_username(self):
        return self.username

    def coerce_error(self, err):
        return f"{type(err).__name__}: {err}"

    def request_json(self, url, **_):
        self.rest_calls.append(url)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path.startswith("/search/issues"):
            q = qs["q"][0]
            items = self.issues
            if "is:issue" in q:
                items = [i for i in items if "pull_request" not in i]
            if "is:pr" in q:
                items = [i for i in items if "pull_request" in i]
            return {"total_count": len(items), "items": items}
        if parsed.path.endswith("/repos"):
            page = int(qs.get("page", ["1"])[0])
            per = int(qs["per_page"][0])
            return self.repos[(page - 1) * per : page * per]
        raise AssertionError(url)

    def request_graphql(self, query, variables, **_):
        self.graphql_calls.append(variables)
        return DISCUSSION_PAYLOAD


@pytest.fixture
def core(monkeypatch):
    fake = FakeCore()
    monkeypatch.setattr(server, "_core", lambda: fake)
    return fake


def _fetch(core, tmp_path, **options):
    return server.fetch(options, {}, ctx={"data_dir": str(tmp_path), "fresh": True})


def test_excluded_matches_full_short_and_patterns():
    assert server._excluded("dmellok/tesserae", {"dmellok/tesserae"}, [])
    assert server._excluded("dmellok/Tesserae", {"tesserae"}, [])
    assert server._excluded("dmellok/tesserae-widget-sky", set(), ["tesserae-widget-*"])
    assert server._excluded("dmellok/tesserae-widget-sky", set(), ["dmellok/*-sky"])
    assert not server._excluded("dmellok/tesserae", set(), ["tesserae-widget-*"])


def test_owners_dedupe_and_strip_at():
    assert server._owners("Dmellok", "@dmellok, seeed-studio") == ["dmellok", "seeed-studio"]


def test_fetch_splits_kinds_counts_and_sorts_by_update(core, tmp_path):
    out = _fetch(core, tmp_path)
    assert out["counts"] == {"issue": 2, "pr": 2, "discussion": 1}
    assert out["by_repo"]["dmellok/tesserae"] == {"issue": 1, "pr": 1, "discussion": 1}
    assert out["by_repo"]["dmellok/tesserae-widgets"] == {"issue": 0, "pr": 1, "discussion": 0}
    assert out["total"] == 5 and out["shown"] == 5
    assert [it["kind"] for it in out["items"]] == ["pr", "discussion", "pr", "issue", "issue"]
    assert out["items"][0]["repo"] == "dmellok/tesserae-widgets"
    assert out["items"][2]["draft"] is True
    disc = out["items"][1]
    assert disc["answerable"] and not disc["answered"] and disc["category"] == "Q&A"
    assert out["items"][-1]["labels"] == ["bug", "help wanted"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(core.rest_calls[0]).query)["q"][0]
    assert q == "is:open archived:false user:dmellok"
    assert core.graphql_calls[0]["q"] == "is:open user:dmellok"


def test_exclusions_apply_to_everything_including_discussions(core, tmp_path):
    out = _fetch(
        core, tmp_path, exclude_repos=["dmellok/tesserae"], exclude_patterns="tesserae-widget-*"
    )
    assert [it["repo"] for it in out["items"]] == ["dmellok/tesserae-widgets"]
    assert out["counts"] == {"issue": 0, "pr": 1, "discussion": 0}


def test_sort_oldest_and_cap(core, tmp_path):
    out = _fetch(core, tmp_path, sort="oldest", max_items=2)
    assert [it["number"] for it in out["items"]] == [1, 3]
    assert out["total"] == 5 and out["shown"] == 2 and out["truncated"] is False


def test_kind_switches_narrow_the_query(core, tmp_path):
    out = _fetch(core, tmp_path, show_prs=False, show_discussions=False)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(core.rest_calls[0]).query)["q"][0]
    assert q.endswith(" is:issue")
    assert {it["kind"] for it in out["items"]} == {"issue"}
    assert not core.graphql_calls


def test_extra_owners_widen_scope(core, tmp_path):
    _fetch(core, tmp_path, extra_owners="seeed-studio")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(core.rest_calls[0]).query)["q"][0]
    assert "user:dmellok user:seeed-studio" in q


def test_no_token_skips_discussions_with_note(monkeypatch, tmp_path):
    fake = FakeCore(token="")
    monkeypatch.setattr(server, "_core", lambda: fake)
    out = _fetch(fake, tmp_path)
    assert out["counts"]["discussion"] == 0
    assert "token" in out["note"]
    assert not fake.graphql_calls


def test_no_username_is_an_error(monkeypatch, tmp_path):
    fake = FakeCore(username="")
    monkeypatch.setattr(server, "_core", lambda: fake)
    out = _fetch(fake, tmp_path)
    assert "username" in out["error"]


def test_rest_failure_surfaces_as_error_when_nothing_else(monkeypatch, tmp_path):
    fake = FakeCore(token="")

    def boom(url, **_):
        raise RuntimeError("rate limited")

    fake.request_json = boom
    monkeypatch.setattr(server, "_core", lambda: fake)
    out = _fetch(fake, tmp_path)
    assert "rate limited" in out["error"] and out["items"] == []


def test_cache_is_reused_and_group_by_is_not_cached(core, tmp_path):
    ctx = {"data_dir": str(tmp_path)}
    first = server.fetch({"group_by": "repo"}, {}, ctx=ctx)
    second = server.fetch({"group_by": "kind"}, {}, ctx=ctx)
    assert len(core.rest_calls) == 1
    assert first["items"] == second["items"]
    assert second["group_by"] == "kind"
    cached = json.loads(next(tmp_path.glob("inbox_*.json")).read_text())
    assert cached["total"] == 5


def test_choices_lists_owned_repos_and_skips_archived(monkeypatch):
    repos = [{"full_name": f"dmellok/r{i}", "archived": i == 3} for i in range(150)]
    fake = FakeCore(repos=repos)
    monkeypatch.setattr(server, "_core", lambda: fake)
    out = server.choices("repo")
    assert len(out) == 149
    assert out[0] == {"value": "dmellok/r0", "label": "dmellok/r0"}
    assert len(fake.rest_calls) == 2
    assert server.choices("other") == []


def test_choices_without_username_is_empty(monkeypatch):
    fake = FakeCore(username="")
    monkeypatch.setattr(server, "_core", lambda: fake)
    assert server.choices("repo") == []
