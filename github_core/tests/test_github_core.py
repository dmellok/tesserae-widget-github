"""Tests for github_core (GitHub App Manifest, RS256 JWT, Installation tokens,
User OAuth + refresh, PAT fallback, legacy settings, error diagnostics) and
multi-account widget integration across all 14 widgets."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(plugin_id: str) -> Any:
    path = REPO_ROOT / plugin_id / "server.py"
    name = f"_test_gh_bundle_{plugin_id}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeSecretBox:
    """Deterministic test box verifying wrap/unwrap roundtrips without leaking plaintext."""

    def wrap(self, plaintext: str) -> str:
        return "enc:test:" + base64.urlsafe_b64encode(plaintext.encode("utf-8")).decode("ascii")

    def unwrap(self, stored: str) -> str:
        if not stored.startswith("enc:test:"):
            return stored
        return base64.urlsafe_b64decode(stored[len("enc:test:") :].encode("ascii")).decode("utf-8")


class _FakeSettingsStore:
    def __init__(self, sections: dict[str, dict[str, Any]] | None = None) -> None:
        self._sections = sections or {}

    def get_section(self, name: str) -> dict[str, Any]:
        return dict(self._sections.get(name, {}))


class _FakeHTTPResp:
    def __init__(self, payload: Any, *, status: int = 200, headers: dict[str, str] | None = None):
        self._body = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else json.dumps(payload).encode("utf-8")
        )
        self.status = status
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> _FakeHTTPResp:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _http_error(
    code: int,
    payload: dict[str, Any] | str = "",
    *,
    headers: dict[str, str] | None = None,
    url: str = "https://api.github.com/test",
) -> urllib.error.HTTPError:
    raw = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload).encode("utf-8")
    )
    return urllib.error.HTTPError(
        url=url,
        code=code,
        msg="Error",
        hdrs=headers or {},  # type: ignore[arg-type]
        fp=io.BytesIO(raw),
    )


@pytest.fixture(scope="session")
def test_rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture
def core_env(tmp_path: Path):
    core = _load_module("github_core")
    core._INSTALL_TOKEN_CACHE.clear()
    core._PENDING_STATES.clear()

    app = Flask(__name__)
    app.secret_key = "test-secret"
    data_dir = tmp_path / "github_core_data"
    data_dir.mkdir()

    plugin = SimpleNamespace(id="github_core", data_dir=data_dir, server_module=core)
    app.config["PLUGIN_REGISTRY"] = {"github_core": plugin}
    app.config["SECRET_BOX"] = _FakeSecretBox()
    app.config["SETTINGS_STORE"] = _FakeSettingsStore()

    with app.app_context():
        yield core, app, data_dir


# ----- JWT & Manifest ------------------------------------------------


def test_build_app_jwt_produces_valid_rs256_structure(core_env, test_rsa_pem):
    core, _, _ = core_env
    token = core.build_app_jwt("123456", test_rsa_pem, now=1_700_000_000)
    parts = token.split(".")
    assert len(parts) == 3

    def _dec(part: str) -> dict[str, Any]:
        pad = "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part + pad))

    hdr = _dec(parts[0])
    body = _dec(parts[1])
    assert hdr == {"alg": "RS256", "typ": "JWT"}
    assert body["iss"] == "123456"
    assert body["iat"] == 1_700_000_000 - 60
    assert body["exp"] == 1_700_000_000 + 540


def test_build_app_manifest_includes_all_readonly_permissions(core_env):
    core, _, _ = core_env
    manifest = core.build_app_manifest("My Tesserae", "http://tesserae.local:5000/")
    assert manifest["name"] == "My Tesserae"
    assert manifest["redirect_url"] == "http://tesserae.local:5000/plugins/github_core/apps/manifest/callback"
    assert manifest["callback_urls"] == ["http://tesserae.local:5000/plugins/github_core/oauth/callback"]
    assert manifest["setup_url"] == "http://tesserae.local:5000/plugins/github_core/apps/installed"
    assert manifest["request_oauth_on_install"] is True
    assert manifest["default_permissions"] == {
        "actions": "read",
        "contents": "read",
        "discussions": "read",
        "issues": "read",
        "metadata": "read",
        "pull_requests": "read",
    }


def test_manifest_conversion_and_encryption_at_rest(core_env, monkeypatch, test_rsa_pem):
    core, _, data_dir = core_env

    def fake_urlopen(req, timeout=15):
        assert "/app-manifests/tempcode123/conversions" in req.full_url
        return _FakeHTTPResp(
            {
                "id": 99001,
                "slug": "tesserae-gh",
                "name": "Tesserae GH",
                "html_url": "https://github.com/apps/tesserae-gh",
                "client_id": "Iv1.abcdef",
                "client_secret": "super-client-secret",
                "pem": test_rsa_pem,
                "owner": {"login": "octocat"},
            }
        )

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    created = core.convert_manifest_code("tempcode123")
    assert created["app_id"] == "99001"
    assert created["slug"] == "tesserae-gh"
    assert created["install_url"] == "https://github.com/apps/tesserae-gh/installations/new"
    assert created["has_oauth"] is True
    assert "pem" not in created
    assert "client_secret" not in created

    raw_disk = (data_dir / "accounts.json").read_text(encoding="utf-8")
    assert "super-client-secret" not in raw_disk
    assert "BEGIN PRIVATE KEY" not in raw_disk
    assert "enc:test:" in raw_disk


# ----- Installations & Multi-Org Token Minting -----------------------


def test_sync_installations_and_mint_cached_tokens(core_env, monkeypatch, test_rsa_pem):
    core, _, _ = core_env
    created_app = core._upsert_app(
        name="Tesserae GH",
        app_id="4242",
        pem=test_rsa_pem,
        client_id="Iv1.123",
        client_secret="sec",
        slug="tesserae-gh",
    )

    calls: list[str] = []

    def fake_urlopen(req, timeout=15):
        calls.append(f"{req.method} {req.full_url}")
        if "/app/installations?" in req.full_url:
            return _FakeHTTPResp(
                [
                    {
                        "id": 1001,
                        "account": {"login": "octocat", "type": "User"},
                        "repository_selection": "all",
                    },
                    {
                        "id": 2002,
                        "account": {"login": "acme-org", "type": "Organization"},
                        "repository_selection": "selected",
                    },
                ]
            )
        if "/app/installations/2002/access_tokens" in req.full_url:
            return _FakeHTTPResp(
                {"token": "ghs_org_token_1", "expires_at": "2099-01-01T00:00:00Z"}
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    synced = core.sync_app_installations(created_app["id"])
    assert len(synced) == 2
    org_acc = next(a for a in synced if a["username"] == "acme-org")
    assert org_acc["auth_type"] == "github_app_installation"
    assert org_acc["account_type"] == "Organization"
    assert org_acc["auth_label"] == "GitHub App · Org"

    # First call mints token; second call hits in-memory cache.
    tok1 = core.get_token(org_acc["id"])
    tok2 = core.get_token(org_acc["id"])
    assert tok1 == "ghs_org_token_1"
    assert tok2 == "ghs_org_token_1"
    assert sum("access_tokens" in c for c in calls) == 1

    # Installation accounts are rejected cleanly when user-scoped endpoints require a user token.
    with pytest.raises(core.GithubCoreError) as exc:
        core.resolve_account(org_acc["id"], require_user_token=True)
    assert exc.value.code == "user_token_required"
    assert "Authorize a user" in exc.value.message


# ----- User OAuth & Auto-Refresh -------------------------------------


def test_user_oauth_exchange_and_automatic_refresh(core_env, monkeypatch, test_rsa_pem):
    core, _, _ = core_env
    created_app = core._upsert_app(
        name="Tesserae GH",
        app_id="5555",
        pem=test_rsa_pem,
        client_id="Iv1.oauth",
        client_secret="oauth-secret",
        slug="tesserae-gh",
    )

    def fake_urlopen(req, timeout=15):
        if "github.com/login/oauth/access_token" in req.full_url:
            body = req.data.decode("utf-8")
            if "grant_type=refresh_token" in body:
                return _FakeHTTPResp(
                    {
                        "access_token": "ghu_refreshed_tok",
                        "refresh_token": "ghr_rotated_refresh",
                        "expires_in": 28800,
                        "refresh_token_expires_in": 15897600,
                    }
                )
            return _FakeHTTPResp(
                {
                    "access_token": "ghu_initial_tok",
                    "refresh_token": "ghr_initial_refresh",
                    "expires_in": 10,  # already within refresh margin
                    "refresh_token_expires_in": 15897600,
                }
            )
        if req.full_url == "https://api.github.com/user":
            return _FakeHTTPResp({"login": "alice"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    acc = core.exchange_oauth_code(created_app["id"], "code123", "http://localhost/cb")
    assert acc["username"] == "alice"
    assert acc["auth_type"] == "github_app_user"

    # Because expires_in=10 < 300s refresh margin, get_token refreshes automatically!
    refreshed_token = core.get_token(acc["id"])
    assert refreshed_token == "ghu_refreshed_tok"


# ----- Legacy fallback & choices -------------------------------------


def test_legacy_settings_fallback_and_account_choices(core_env):
    core, app, _ = core_env
    assert core.account_choices(require_auth=False) == [
        {"value": "", "label": "Public (no account connected)"}
    ]
    assert core.account_choices(require_auth=True)[0]["note"] is True

    app.config["SETTINGS_STORE"] = _FakeSettingsStore(
        {"plugins": {"github_core": {"username": "legacyuser", "token_secret": "ghp_legacy_123456"}}}
    )
    assert core.get_token() == "ghp_legacy_123456"
    assert core.get_username() == "legacyuser"
    choices = core.account_choices(require_auth=True)
    assert choices == [{"value": "", "label": "Default (@legacyuser, legacy settings)"}]

    # Adding a real account supersedes legacy fallback cleanly.
    acc = core.create_account("Work PAT", "ghp_work_987654321")
    choices2 = core.account_choices(require_auth=True)
    assert choices2[0]["value"] == ""
    assert choices2[1]["value"] == acc["id"]


# ----- Error diagnostics ---------------------------------------------


def test_coerce_error_produces_clear_diagnostics(core_env):
    core, _, _ = core_env
    msg401 = core.coerce_error(_http_error(401, {"message": "Bad credentials"}), account_name="Work")
    assert "401" in msg401 and "Work" in msg401 and "/plugins/github_core/" in msg401

    reset_ts = str(int(time.time()) + 600)
    msg_rl = core.coerce_error(
        _http_error(
            403,
            {"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset_ts},
        ),
        account_name="",
    )
    assert "rate limit" in msg_rl.lower() and "UTC" in msg_rl

    msg404 = core.coerce_error(
        _http_error(404, {"message": "Not Found"}), account_name="Acme Org", repo="acme/secret"
    )
    assert "acme/secret" in msg404 and "Acme Org" in msg404

    gql_msg = core.coerce_graphql_error(
        {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}]},
        account_name="Acme Org",
        auth_type="github_app_installation",
    )
    assert gql_msg is not None and "authorize a user" in gql_msg.lower()


# ----- All 14 widgets: choices("account") & error surfacing ----------


ALL_WIDGETS = [
    "github_actions",
    "github_activity",
    "github_activity_heatmap",
    "github_ci_status",
    "github_commit_cadence",
    "github_contributions",
    "github_inbox",
    "github_issue_queue",
    "github_pr_count",
    "github_pr_queue",
    "github_releases",
    "github_repo",
    "github_star_count",
    "github_star_growth",
    "github_streak",
]


@pytest.mark.parametrize("widget_id", ALL_WIDGETS)
def test_every_widget_exposes_account_choices_and_surfaces_missing_account(
    core_env, tmp_path, widget_id
):
    core, _, _ = core_env
    core.create_account("Personal", "ghp_personal_123456789")
    mod = _load_module(widget_id)

    rows = mod.choices("account", {})
    assert any(r["value"] == "personal" for r in rows), widget_id

    # Explicit non-existent account produces an actionable UI error (not a crash or silent empty).
    opts: dict[str, Any] = {"account": "deleted-account", "repo": "a/b", "repos": "a/b", "user": "alice"}
    out = mod.fetch(opts, {}, ctx={"data_dir": str(tmp_path / widget_id), "fresh": True})
    assert "error" in out and "deleted-account" in out["error"], (widget_id, out)


def test_pr_queue_actions_releases_surface_api_errors_instead_of_swallowing(core_env, monkeypatch, tmp_path):
    core, _, _ = core_env
    acc = core.create_account("Work", "ghp_work_123456789")
    # Set username directly in stored record for test.
    data = core._load_raw()
    data["accounts"][0]["username"] = "alice"
    core._save_raw(data)

    def boom(req, timeout=15):
        raise _http_error(403, {"message": "Resource not accessible by personal access token"})

    monkeypatch.setattr(core.urllib.request, "urlopen", boom)

    for wid, opts in [
        ("github_actions", {"account": acc["id"], "repos": "acme/api"}),
        ("github_releases", {"account": acc["id"], "repos": "acme/api"}),
        ("github_pr_queue", {"account": acc["id"]}),
        ("github_issue_queue", {"account": acc["id"]}),
    ]:
        mod = _load_module(wid)
        out = mod.fetch(opts, {}, ctx={"data_dir": str(tmp_path / wid), "fresh": True})
        assert out.get("error"), (wid, out)
        assert "403" in out["error"] and "Work" in out["error"]


def test_github_inbox_splits_search_query_on_422_for_fine_grained_pats(core_env, monkeypatch, tmp_path):
    core, _, _ = core_env
    acc = core.create_account("FineGrained", "github_pat_1234567890abcdef")
    data = core._load_raw()
    data["accounts"][0]["username"] = "alice"
    core._save_raw(data)

    seen_queries: list[str] = []

    def fake_urlopen(req, timeout=15):
        import urllib.parse

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        q = qs["q"][0]
        seen_queries.append(q)
        if "is:issue" not in q and "is:pr" not in q:
            raise _http_error(422, {"message": "Validation Failed"})
        if "is:issue" in q:
            return _FakeHTTPResp(
                {
                    "total_count": 1,
                    "items": [
                        {
                            "title": "Bug",
                            "number": 1,
                            "repository_url": "https://api.github.com/repos/alice/app",
                            "html_url": "https://github.com/alice/app/issues/1",
                            "created_at": "2026-10-01T00:00:00Z",
                            "updated_at": "2026-10-02T00:00:00Z",
                            "user": {"login": "bob"},
                            "comments": 0,
                            "labels": [],
                        }
                    ],
                }
            )
        return _FakeHTTPResp({"total_count": 0, "items": []})

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    inbox = _load_module("github_inbox")
    out = inbox.fetch(
        {"account": acc["id"], "show_discussions": False},
        {},
        ctx={"data_dir": str(tmp_path / "inbox"), "fresh": True},
    )
    assert not out.get("error")
    assert out["counts"]["issue"] == 1
    assert any("is:issue" in q for q in seen_queries)
    assert any("is:pr" in q for q in seen_queries)


def test_admin_blueprint_manifest_and_oauth_routes(core_env, monkeypatch, test_rsa_pem):
    core, app, _ = core_env
    app.register_blueprint(core.blueprint(), url_prefix="/plugins/github_core")

    def fake_urlopen(req, timeout=15):
        if "/app-manifests/mcode/conversions" in req.full_url:
            return _FakeHTTPResp(
                {
                    "id": 7777,
                    "slug": "tesserae-demo",
                    "name": "Tesserae Demo",
                    "html_url": "https://github.com/apps/tesserae-demo",
                    "client_id": "Iv1.demo",
                    "client_secret": "demo-secret",
                    "pem": test_rsa_pem,
                    "owner": {"login": "octocat"},
                }
            )
        if "github.com/login/oauth/access_token" in req.full_url:
            return _FakeHTTPResp(
                {
                    "access_token": "ghu_demo_tok",
                    "refresh_token": "ghr_demo_ref",
                    "expires_in": 28800,
                }
            )
        if req.full_url == "https://api.github.com/user":
            return _FakeHTTPResp({"login": "octocat"})
        if "/app/installations?" in req.full_url:
            return _FakeHTTPResp(
                [
                    {
                        "id": 3003,
                        "account": {"login": "demo-org", "type": "Organization"},
                        "repository_selection": "all",
                    }
                ]
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    client = app.test_client()

    # 1) Start 1-click Manifest flow
    r_start = client.post("/plugins/github_core/apps/manifest/start", data={"name": "Tesserae Demo"})
    assert r_start.status_code == 200
    html = r_start.get_data(as_text=True)
    assert "https://github.com/settings/apps/new?state=" in html
    state = html.split("state=", 1)[1].split('"', 1)[0]

    # 2) Complete Manifest callback
    r_cb = client.get(f"/plugins/github_core/apps/manifest/callback?code=mcode&state={state}")
    assert r_cb.status_code == 302
    apps = core.list_apps()
    assert len(apps) == 1 and apps[0]["app_id"] == "7777"

    # 3) Sync org installations
    r_sync = client.post(f"/plugins/github_core/apps/{apps[0]['id']}/sync")
    assert r_sync.status_code == 302
    assert any(a["username"] == "demo-org" for a in core.list_accounts())

    # 4) Authorize user via OAuth
    r_oa = client.get(f"/plugins/github_core/apps/{apps[0]['id']}/oauth/start")
    assert r_oa.status_code == 302
    loc = r_oa.headers["Location"]
    assert "github.com/login/oauth/authorize" in loc
    oauth_state = loc.split("state=", 1)[1].split("&", 1)[0]

    r_oa_cb = client.get(f"/plugins/github_core/oauth/callback?code=ocode&state={oauth_state}")
    assert r_oa_cb.status_code == 302
    accounts = core.list_accounts()
    assert any(a["auth_type"] == "github_app_user" and a["username"] == "octocat" for a in accounts)
    assert any(a["auth_type"] == "github_app_installation" and a["username"] == "demo-org" for a in accounts)


# ----- Multiple GitHub Apps & Multiple PATs --------------------------


def test_manifest_batch_creates_multiple_github_apps_in_sequence(core_env, monkeypatch, test_rsa_pem):
    core, app, _ = core_env
    app.register_blueprint(core.blueprint(), url_prefix="/plugins/github_core")

    def fake_urlopen(req, timeout=15):
        if "/app-manifests/code-personal/conversions" in req.full_url:
            return _FakeHTTPResp(
                {
                    "id": 8001,
                    "slug": "tesserae-personal",
                    "name": "Tesserae Personal",
                    "html_url": "https://github.com/apps/tesserae-personal",
                    "client_id": "Iv1.pers",
                    "client_secret": "sec-pers",
                    "pem": test_rsa_pem,
                    "owner": {"login": "alice"},
                }
            )
        if "/app-manifests/code-work/conversions" in req.full_url:
            return _FakeHTTPResp(
                {
                    "id": 8002,
                    "slug": "tesserae-acme",
                    "name": "Tesserae Acme",
                    "html_url": "https://github.com/apps/tesserae-acme",
                    "client_id": "Iv1.acme",
                    "client_secret": "sec-acme",
                    "pem": test_rsa_pem,
                    "owner": {"login": "acme-org"},
                }
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    client = app.test_client()

    # Submit 2 GitHub Apps in a single manifest start form.
    r1 = client.post(
        "/plugins/github_core/apps/manifest/start",
        data={"name": ["Tesserae Personal", "Tesserae Acme"], "org": ["", "acme-org"]},
    )
    assert r1.status_code == 200
    html1 = r1.get_data(as_text=True)
    assert "1 of 2" in html1
    assert "https://github.com/settings/apps/new?state=" in html1
    state1 = html1.split("state=", 1)[1].split('"', 1)[0]

    # Completing App 1 automatically renders the manifest POST page for App 2 (org-owned).
    r2 = client.get(f"/plugins/github_core/apps/manifest/callback?code=code-personal&state={state1}")
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "2 of 2" in html2
    assert "https://github.com/organizations/acme-org/settings/apps/new?state=" in html2
    state2 = html2.split("state=", 1)[1].split('"', 1)[0]

    # Completing App 2 redirects back to GitHub Core with both apps persisted.
    r3 = client.get(f"/plugins/github_core/apps/manifest/callback?code=code-work&state={state2}")
    assert r3.status_code == 302
    apps = core.list_apps()
    assert [a["app_id"] for a in apps] == ["8001", "8002"]
    assert [a["owner"] for a in apps] == ["alice", "acme-org"]


def test_manual_batch_edit_test_sync_all_and_delete_multiple_apps(core_env, monkeypatch, test_rsa_pem):
    core, app, _ = core_env
    app.register_blueprint(core.blueprint(), url_prefix="/plugins/github_core")

    def fake_urlopen(req, timeout=15):
        auth = req.headers.get("Authorization", "")
        # Distinguish apps by JWT iss claim.
        if req.full_url.endswith("/app"):
            jwt_part = auth.split(" ", 1)[1].split(".")[1]
            pad = "=" * (-len(jwt_part) % 4)
            iss = json.loads(base64.urlsafe_b64decode(jwt_part + pad))["iss"]
            if iss == "9001":
                return _FakeHTTPResp(
                    {
                        "slug": "personal-app",
                        "name": "Personal App",
                        "html_url": "https://github.com/apps/personal-app",
                        "owner": {"login": "alice"},
                        "installations_count": 1,
                    }
                )
            return _FakeHTTPResp(
                {
                    "slug": "work-app",
                    "name": "Work App",
                    "html_url": "https://github.com/apps/work-app",
                    "owner": {"login": "acme-org"},
                    "installations_count": 1,
                }
            )
        if "/app/installations?" in req.full_url:
            jwt_part = auth.split(" ", 1)[1].split(".")[1]
            pad = "=" * (-len(jwt_part) % 4)
            iss = json.loads(base64.urlsafe_b64decode(jwt_part + pad))["iss"]
            if iss == "9001":
                return _FakeHTTPResp(
                    [{"id": 501, "account": {"login": "alice", "type": "User"}, "repository_selection": "all"}]
                )
            return _FakeHTTPResp(
                [{"id": 502, "account": {"login": "alice", "type": "User"}, "repository_selection": "selected"}]
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    client = app.test_client()

    # Register 2 GitHub Apps manually in a single submission.
    r_man = client.post(
        "/plugins/github_core/apps/manual",
        data={
            "name": ["Personal App", "Work App"],
            "app_id": ["9001", "9002"],
            "client_id": ["Iv1.p", "Iv1.w"],
            "client_secret": ["sec-p", "sec-w"],
            "pem": [test_rsa_pem, test_rsa_pem],
        },
    )
    assert r_man.status_code == 302
    apps = core.list_apps()
    assert len(apps) == 2
    assert {a["app_id"] for a in apps} == {"9001", "9002"}

    # Edit App 2 without re-entering secrets; duplicate App ID is rejected.
    app2_id = next(a["id"] for a in apps if a["app_id"] == "9002")
    with pytest.raises(ValueError, match="already registered"):
        core.update_app(app2_id, app_id="9001")
    r_edit = client.post(
        f"/plugins/github_core/apps/{app2_id}/edit",
        data={"name": "Work App Renamed", "app_id": "9002", "client_id": "Iv1.w2", "client_secret": "", "pem": ""},
    )
    assert r_edit.status_code == 302
    refreshed_app2 = next(a for a in core.list_apps() if a["id"] == app2_id)
    assert refreshed_app2["name"] == "Work App Renamed"
    assert refreshed_app2["client_id"] == "Iv1.w2"
    assert refreshed_app2["has_oauth"] is True

    # Test App JWT directly.
    test_res = core.test_app(app2_id)
    assert test_res["ok"] is True
    assert test_res["owner"] == "acme-org"

    # Sync all apps in one action; same login across 2 apps gets disambiguated names & dropdown labels.
    r_sync_all = client.post("/plugins/github_core/apps/sync-all")
    assert r_sync_all.status_code == 302
    accounts = core.list_accounts()
    assert len(accounts) == 2
    assert len({a["name"] for a in accounts}) == 2
    choices = core.account_choices(require_auth=False)
    labels = [c["label"] for c in choices]
    assert any("Personal App" in lbl for lbl in labels)
    assert any("Work App Renamed" in lbl for lbl in labels)

    # Deleting App 1 removes only App 1's linked accounts, keeping App 2 intact.
    app1_id = next(a["id"] for a in apps if a["app_id"] == "9001")
    assert core.delete_app(app1_id) is True
    assert [a["id"] for a in core.list_apps()] == [app2_id]
    assert all(a["app_ref"] == app2_id for a in core.list_accounts())


def test_multiple_pats_batch_verify_default_and_test_all(core_env, monkeypatch):
    core, app, data_dir = core_env
    app.register_blueprint(core.blueprint(), url_prefix="/plugins/github_core")

    def fake_urlopen(req, timeout=15):
        auth = req.headers.get("Authorization", "")
        if req.full_url == "https://api.github.com/user":
            if "github_pat_personal_123456" in auth:
                return _FakeHTTPResp({"login": "alice"}, headers={"X-RateLimit-Remaining": "4999"})
            if "ghp_work_6543210000" in auth:
                return _FakeHTTPResp({"login": "alice-work"}, headers={"X-RateLimit-Remaining": "4990"})
            raise _http_error(401, {"message": "Bad credentials"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    client = app.test_client()

    # Add 2 PATs (one fine-grained, one classic) in one request with auto-verify on save.
    r_add = client.post(
        "/plugins/github_core/accounts/pat",
        data={
            "name": ["Personal PAT", "Work PAT", ""],
            "pat": ["github_pat_personal_123456", "ghp_work_6543210000", ""],
            "username": ["", "", ""],
            "verify_on_save": "1",
        },
    )
    assert r_add.status_code == 302
    accounts = core.list_accounts()
    assert len(accounts) == 2
    assert accounts[0]["name"] == "Personal PAT"
    assert accounts[0]["username"] == "alice"
    assert accounts[0]["pat_kind"] == "Fine-grained"
    assert accounts[1]["name"] == "Work PAT"
    assert accounts[1]["username"] == "alice-work"
    assert accounts[1]["pat_kind"] == "Classic"

    # Secrets are encrypted on disk.
    raw_disk = (data_dir / "accounts.json").read_text(encoding="utf-8")
    assert "github_pat_personal_123456" not in raw_disk
    assert "ghp_work_6543210000" not in raw_disk

    # Promote the second PAT to be the Default account.
    r_def = client.post(f"/plugins/github_core/accounts/{accounts[1]['id']}/default")
    assert r_def.status_code == 302
    reordered = core.list_accounts()
    assert reordered[0]["id"] == accounts[1]["id"]
    assert core.get_username() == "alice-work"

    # Edit PAT username & test all accounts.
    r_edit = client.post(
        f"/plugins/github_core/accounts/{accounts[0]['id']}/edit",
        data={"name": "Personal Fine-Grained", "username": "@alice-custom", "pat": ""},
    )
    assert r_edit.status_code == 302
    edited = next(a for a in core.list_accounts() if a["id"] == accounts[0]["id"])
    assert edited["name"] == "Personal Fine-Grained"
    assert edited["username"] == "alice-custom"

    r_test_all = client.post("/plugins/github_core/accounts/test-all")
    assert r_test_all.status_code == 302
    summary = core.test_all_accounts()
    assert summary["tested"] == 2 and summary["ok_count"] == 2 and summary["failed"] == []


def test_admin_index_template_renders_with_multiple_apps_and_pats(core_env, tmp_path, test_rsa_pem):
    core, app, _ = core_env
    # Provide lightweight Tesserae base/component stubs so Jinja renders index.html end-to-end.
    tpl_dir = tmp_path / "base_tpls"
    tpl_dir.mkdir()
    (tpl_dir / "_base.html").write_text("{% block title %}{% endblock %}{% block main %}{% endblock %}", encoding="utf-8")
    (tpl_dir / "_components.html").write_text(
        "{% macro card_head(t, icon_name='', level=2, collapse='') %}<h2>{{ t }}</h2>{% endmacro %}"
        "{% macro button(l, icon='') %}<button type='submit'>{{ l }}</button>{% endmacro %}"
        "{% macro empty_state(i, t, blurb='') %}<div>{{ t }} {{ blurb }}</div>{% endmacro %}",
        encoding="utf-8",
    )
    app.template_folder = str(tpl_dir)
    app.register_blueprint(core.blueprint(), url_prefix="/plugins/github_core")

    core._upsert_app(name="App One", app_id="1001", pem=test_rsa_pem, slug="app-one", client_id="Iv1.1", client_secret="s1")
    core._upsert_app(name="App Two", app_id="1002", pem=test_rsa_pem, slug="app-two", client_id="Iv1.2", client_secret="s2")
    core.create_account("Personal PAT", "github_pat_1111111111111111", username="alice")
    core.create_account("Work PAT", "ghp_2222222222222222", username="alice-work")

    client = app.test_client()
    resp = client.get("/plugins/github_core/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "App One" in body and "App Two" in body
    assert "Personal PAT" in body and "Work PAT" in body
    assert "Add another GitHub App" in body
    assert "Add another PAT" in body
    assert "Sync all apps" in body
    assert "Set default" in body
    assert "Test all" in body


def test_org_github_app_installation_works_across_org_and_user_widgets(
    core_env, monkeypatch, tmp_path, test_rsa_pem
):
    core, _, _ = core_env
    created_app = core._upsert_app(
        name="Acme Org App",
        app_id="6001",
        pem=test_rsa_pem,
        slug="acme-org-app",
    )

    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=15):
        seen_urls.append(req.full_url)
        if "/app/installations?" in req.full_url:
            return _FakeHTTPResp(
                [
                    {
                        "id": 4004,
                        "account": {"login": "acme-org", "type": "Organization"},
                        "repository_selection": "all",
                    }
                ]
            )
        if "/app/installations/4004/access_tokens" in req.full_url:
            return _FakeHTTPResp({"token": "ghs_acme_tok", "expires_at": "2099-01-01T00:00:00Z"})
        if "/installation/repositories" in req.full_url:
            return _FakeHTTPResp(
                {
                    "total_count": 2,
                    "repositories": [
                        {
                            "name": "core",
                            "owner": {"login": "acme-org"},
                            "stargazers_count": 42,
                            "fork": False,
                            "html_url": "https://github.com/acme-org/core",
                        },
                        {
                            "name": "forked",
                            "owner": {"login": "acme-org"},
                            "stargazers_count": 99,
                            "fork": True,
                            "html_url": "https://github.com/acme-org/forked",
                        },
                    ],
                }
            )
        if "/search/issues?" in req.full_url:
            import urllib.parse

            q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["q"][0]
            if "-is:draft" in q:
                return _FakeHTTPResp(
                    {
                        "total_count": 2,
                        "items": [
                            {
                                "title": "Ship v2",
                                "number": 12,
                                "repository_url": "https://api.github.com/repos/acme-org/core",
                                "created_at": "2026-11-20T00:00:00Z",
                                "updated_at": "2026-11-21T00:00:00Z",
                                "user": {"login": "alice"},
                                "comments": 1,
                                "draft": False,
                            }
                        ],
                    }
                )
            return _FakeHTTPResp({"total_count": 1, "items": []})
        if "/orgs/acme-org/events" in req.full_url:
            return _FakeHTTPResp(
                [
                    {
                        "type": "PushEvent",
                        "repo": {"name": "acme-org/core"},
                        "created_at": "2026-11-22T10:00:00Z",
                        "payload": {"size": 3},
                    }
                ]
            )
        if req.full_url.endswith("/graphql"):
            return _FakeHTTPResp(
                {
                    "data": {
                        "user": {
                            "contributionsCollection": {
                                "contributionCalendar": {
                                    "totalContributions": 5,
                                    "weeks": [
                                        {
                                            "contributionDays": [
                                                {
                                                    "date": "2026-11-22",
                                                    "contributionCount": 5,
                                                    "contributionLevel": "FOURTH_QUARTILE",
                                                }
                                            ]
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    org_acc = core.sync_app_installations(created_app["id"])[0]
    acc_id = org_acc["id"]

    # 1) Star Count sums stars across the org's installation repos (skipping forks).
    star_out = _load_module("github_star_count").fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "stars"), "fresh": True}
    )
    assert not star_out.get("error"), star_out
    assert star_out["total_stars"] == 42 and star_out["total_repos"] == 1

    # 2) PR Queue & PR Count query org PRs (Ready vs Draft) without requiring a user token.
    prq_out = _load_module("github_pr_queue").fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "prq"), "fresh": True}
    )
    assert not prq_out.get("error"), prq_out
    assert prq_out["yours_label"] == "Ready" and len(prq_out["yours"]) == 1

    prc_out = _load_module("github_pr_count").fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "prc"), "fresh": True}
    )
    assert not prc_out.get("error"), prc_out
    assert prc_out["total"] == 3 and prc_out["yours_label"] == "Ready"

    # 3) Activity & Activity Heatmap use /orgs/{org}/events.
    act_out = _load_module("github_activity").fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "act"), "fresh": True}
    )
    assert not act_out.get("error") and act_out["count"] == 1
    assert any("/orgs/acme-org/events" in u for u in seen_urls)

    # 4) Contributions & Streak explain why an org has no calendar, AND work when Username is set.
    contrib_mod = _load_module("github_contributions")
    contrib_org = contrib_mod.fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "c1"), "fresh": True}
    )
    assert "organizations" in contrib_org["error"].lower()
    contrib_user = contrib_mod.fetch(
        {"account": acc_id, "user": "alice"},
        {},
        ctx={"data_dir": str(tmp_path / "c2"), "fresh": True},
    )
    assert not contrib_user.get("error") and contrib_user["total"] == 5

    streak_mod = _load_module("github_streak")
    streak_org = streak_mod.fetch(
        {"account": acc_id}, {}, ctx={"data_dir": str(tmp_path / "s1"), "fresh": True}
    )
    assert "organizations" in streak_org["error"].lower()
    streak_user = streak_mod.fetch(
        {"account": acc_id, "user": "alice"},
        {},
        ctx={"data_dir": str(tmp_path / "s2"), "fresh": True},
    )
    assert not streak_user.get("error") and streak_user["current_streak"] == 1


def test_github_issue_queue_personal_org_and_org_user_modes(
    core_env, monkeypatch, tmp_path, test_rsa_pem
):
    core, _, _ = core_env
    pat_acc = core.create_account("Personal PAT", "ghp_issue_test_123456", username="alice")
    org_app = core._upsert_app(name="Acme App", app_id="6100", pem=test_rsa_pem, slug="acme-app")

    queries: list[str] = []

    def fake_urlopen(req, timeout=15):
        if "/app/installations?" in req.full_url:
            return _FakeHTTPResp(
                [
                    {
                        "id": 4100,
                        "account": {"login": "acme-org", "type": "Organization"},
                        "repository_selection": "all",
                    }
                ]
            )
        if "/app/installations/4100/access_tokens" in req.full_url:
            return _FakeHTTPResp({"token": "ghs_issue_org_tok", "expires_at": "2099-01-01T00:00:00Z"})
        if "/search/issues?" in req.full_url:
            import urllib.parse

            q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["q"][0]
            queries.append(q)
            if "no:assignee" in q and "-no:assignee" not in q:
                return _FakeHTTPResp(
                    {
                        "total_count": 1,
                        "items": [
                            {
                                "title": "Fix login timeout",
                                "number": 42,
                                "repository_url": "https://api.github.com/repos/acme-org/web",
                                "created_at": "2026-11-10T00:00:00Z",
                                "updated_at": "2026-11-21T00:00:00Z",
                                "user": {"login": "bob"},
                                "comments": 3,
                                "labels": [{"name": "bug"}, {"name": "p1"}],
                            }
                        ],
                    }
                )
            if "assignee:alice" in q and "-assignee:alice" not in q:
                return _FakeHTTPResp(
                    {
                        "total_count": 1,
                        "items": [
                            {
                                "title": "Add dark mode",
                                "number": 7,
                                "repository_url": "https://api.github.com/repos/alice/app",
                                "created_at": "2026-11-18T00:00:00Z",
                                "updated_at": "2026-11-20T00:00:00Z",
                                "user": {"login": "alice"},
                                "comments": 0,
                                "labels": [{"name": "enhancement"}],
                            }
                        ],
                    }
                )
            return _FakeHTTPResp({"total_count": 0, "items": []})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)
    org_acc = core.sync_app_installations(org_app["id"])[0]
    mod = _load_module("github_issue_queue")

    # 1) Personal account -> Assigned + Opened (excluding duplicates already assigned).
    out_personal = mod.fetch(
        {"account": pat_acc["id"]}, {}, ctx={"data_dir": str(tmp_path / "iq_p"), "fresh": True}
    )
    assert not out_personal.get("error")
    assert len(out_personal["assigned"]) == 1
    assert out_personal["assigned"][0]["labels"] == ["enhancement"]
    assert "is:issue is:open assignee:alice" in queries
    assert "is:issue is:open author:alice -assignee:alice" in queries

    # 2) Organization App account (blank user) -> Unassigned vs Assigned across org.
    out_org = mod.fetch(
        {"account": org_acc["id"]}, {}, ctx={"data_dir": str(tmp_path / "iq_o"), "fresh": True}
    )
    assert not out_org.get("error")
    assert out_org["assigned_label"] == "Unassigned"
    assert out_org["opened_label"] == "Assigned"
    assert out_org["assigned"][0]["number"] == 42
    assert out_org["assigned"][0]["labels"] == ["bug", "p1"]
    assert "is:issue is:open no:assignee org:acme-org" in queries
    assert "is:issue is:open -no:assignee org:acme-org" in queries

    # 3) Organization App account + explicit Username -> scoped to user inside that org.
    out_org_user = mod.fetch(
        {"account": org_acc["id"], "user": "alice"},
        {},
        ctx={"data_dir": str(tmp_path / "iq_ou"), "fresh": True},
    )
    assert not out_org_user.get("error")
    assert "assigned_label" not in out_org_user
    assert "is:issue is:open org:acme-org assignee:alice" in queries
    assert "is:issue is:open org:acme-org author:alice -assignee:alice" in queries






def test_github_actions_blank_repos_scans_most_recently_pushed_repos(core_env, monkeypatch, tmp_path):
    core, _, _ = core_env
    acc = core.create_account("Personal PAT", "ghp_actions_auto_123456", username="alice")

    calls: list[str] = []
    # Deliberately unsorted to prove list_accessible_repos orders by pushed_at.
    user_repos = [
        {"full_name": "alice/old", "pushed_at": "2025-01-01T00:00:00Z"},
        {"full_name": "alice/newest", "pushed_at": "2026-11-20T00:00:00Z"},
        {"full_name": "alice/archived", "pushed_at": "2026-11-21T00:00:00Z", "archived": True},
        {"full_name": "alice/no-ci", "pushed_at": "2026-11-19T00:00:00Z"},
        {"full_name": "alice/mid", "pushed_at": "2026-06-01T00:00:00Z"},
    ]

    def run(name: str, n: int) -> dict[str, Any]:
        return {
            "name": name,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "run_number": n,
            "run_started_at": "2026-11-20T10:00:00Z",
            "updated_at": f"2026-11-20T10:0{n}:00Z",
        }

    def fake_urlopen(req, timeout=15):
        url = req.full_url
        calls.append(url)
        if "/user/repos?" in url:
            return _FakeHTTPResp(user_repos)
        if "/repos/alice/newest/actions/runs" in url:
            return _FakeHTTPResp({"workflow_runs": [run("CI", 2)]})
        if "/repos/alice/no-ci/actions/runs" in url:
            return _FakeHTTPResp({"workflow_runs": []})
        if "/repos/alice/mid/actions/runs" in url:
            return _FakeHTTPResp({"workflow_runs": [run("Deploy", 1)]})
        raise AssertionError(f"unexpected call {url}")

    monkeypatch.setattr(core.urllib.request, "urlopen", fake_urlopen)

    assert core.list_accessible_repos(acc["id"]) == [
        "alice/newest",
        "alice/no-ci",
        "alice/mid",
        "alice/old",
    ]

    mod = _load_module("github_actions")
    data_dir = str(tmp_path / "actions_auto")

    # max_repos=2 -> only newest + no-ci are scanned; no-ci is skipped quietly.
    out = mod.fetch({"account": acc["id"], "repos": "", "max_repos": 2}, {}, ctx={"data_dir": data_dir, "fresh": True})
    assert not out.get("error"), out
    assert out["auto"] is True and out["scanned"] == 2
    assert [r["repo"] for r in out["runs"]] == ["alice/newest"]
    assert not out.get("errors")
    assert not any("/repos/alice/mid/" in c for c in calls)

    # Widening the cap reuses the cached repo list (no second /user/repos call).
    calls.clear()
    out = mod.fetch({"account": acc["id"], "max_repos": 3}, {}, ctx={"data_dir": data_dir, "fresh": True})
    assert {r["repo"] for r in out["runs"]} == {"alice/newest", "alice/mid"}
    assert not any("/user/repos?" in c for c in calls)

    # Cap is clamped to MAX_REPOS_CAP.
    calls.clear()
    mod.fetch({"account": acc["id"], "max_repos": 999}, {}, ctx={"data_dir": data_dir, "fresh": True})
    assert sum("/actions/runs" in c for c in calls) == 4  # all 4 non-archived repos (< cap)

    # Non-blank but invalid Repos still asks for owner/repo instead of scanning.
    out = mod.fetch({"account": acc["id"], "repos": "not a repo"}, {}, ctx={"data_dir": data_dir, "fresh": True})
    assert "owner/repo" in out["error"]


def test_github_actions_blank_repos_without_account_or_username_explains(core_env, tmp_path):
    mod = _load_module("github_actions")
    out = mod.fetch({"repos": ""}, {}, ctx={"data_dir": str(tmp_path / "actions_pub"), "fresh": True})
    assert out["runs"] == []
    assert "No repos found" in out["error"] and "GitHub Core" in out["error"]
