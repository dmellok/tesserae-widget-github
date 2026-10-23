"""The options panels of every widget in this repo follow the same rules.

A handful of options always visible, the rest revealed by `show_if` or
tucked under More options (`advanced`), advanced ones last, conditions
that point at an earlier option, and the same plain labels for the same
thing across widgets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = sorted(ROOT.glob("github_*/plugin.json"))
MAX_UP_FRONT = 6


def _options(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("cell_options", [])


def _up_front(opts: list[dict]) -> list[str]:
    return [o["name"] for o in opts if "show_if" not in o and not o.get("advanced")]


def test_every_widget_is_checked():
    assert len(MANIFESTS) == 15


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_few_options_up_front(path):
    assert len(_up_front(_options(path))) <= MAX_UP_FRONT


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_advanced_options_come_last(path):
    flags = [bool(o.get("advanced")) for o in _options(path)]
    assert flags == sorted(flags)


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_show_if_points_at_an_earlier_option(path):
    seen: set[str] = set()
    for o in _options(path):
        for key in (o.get("show_if") or {}):
            assert key in seen, (o["name"], key)
        seen.add(o["name"])


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_labels_are_plain(path):
    for o in _options(path):
        texts = [o.get("label", ""), o.get("help", "")]
        texts += [c.get("label", "") for c in o.get("choices", []) if isinstance(c, dict)]
        for text in texts:
            assert "—" not in text, o["name"]
        assert "(" not in o.get("label", ""), o["name"]


def test_shared_labels_match_across_widgets():
    shared = {"user": "Username", "repos": "Repos", "repo": "Repo"}
    for path in MANIFESTS:
        for o in _options(path):
            if o["name"] in shared:
                assert o["label"] == shared[o["name"]], (path.parent.name, o["name"])


def test_inbox_panel():
    opts = _options(ROOT / "github_inbox" / "plugin.json")
    assert _up_front(opts) == [
        "user",
        "show_issues",
        "show_prs",
        "show_discussions",
        "group_by",
        "max_items",
    ]
    assert [o["name"] for o in opts if o.get("advanced")] == [
        "extra_owners",
        "exclude_repos",
        "exclude_patterns",
        "sort",
        "density",
        "show_labels",
    ]
