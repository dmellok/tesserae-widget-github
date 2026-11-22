"""github_core: multi-account GitHub App + OAuth + PAT store & HTTP client.

No widget cell of its own; sibling ``github_*`` widgets reach in via the
plugin registry (``current_app.config["PLUGIN_REGISTRY"].get("github_core").server_module``)
and pass an optional ``account_id`` to ``resolve_account`` / ``request_json`` /
``request_graphql`` / ``headers`` / ``get_token`` / ``get_username``.

Authentication modes supported per saved account:
  * ``github_app_user`` — User-to-Server OAuth token (``ghu_...``) issued by a
    registered GitHub App, automatically refreshed with its ``ghr_...`` refresh
    token before expiry. Works for all 14 widgets including GraphQL user stats,
    contributions, streak, PR queue, discussions, and private repos.
  * ``github_app_installation`` — GitHub App Installation (personal or
    organization), auto-discovered via ``GET /app/installations``. Signs short-
    lived RS256 App JWTs using ``cryptography`` and mints 1-hour Installation
    Access Tokens (``ghs_...``) on demand. Ideal for org & repository widgets.
  * ``pat`` — Optional Personal Access Token fallback (classic or fine-grained),
    plus transparent read fallback for legacy ``SETTINGS_STORE`` credentials so
    existing installations keep working without interruption.

All secrets (PEM keys, client secrets, OAuth tokens, PATs) are encrypted at
rest in ``<data_dir>/accounts.json`` via Tesserae's ``SecretBox``.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    has_app_context,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response

try:
    from app.plugin_http import decode_body as _tesserae_decode_body
except ImportError:  # pragma: no cover - standalone unit tests outside Tesserae
    _tesserae_decode_body = None

try:
    from app.secret_box import SecretBox, SecretBoxError
except ImportError:  # pragma: no cover - standalone unit tests outside Tesserae

    class SecretBoxError(ValueError):  # type: ignore[no-redef]
        pass

    SecretBox = None  # type: ignore[assignment,misc]


GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"
USER_AGENT = "tesserae/0.2 (+github_core)"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
HTTP_TIMEOUT_S = 15

_SCHEMA_VERSION = 2
_ERROR_DETAIL_BYTES = 4096
_STATE_TTL_S = 900  # 15 min for OAuth / App Manifest flows
_TOKEN_REFRESH_MARGIN_S = 300  # refresh 5 min before expiry
_LEGACY_ACCOUNT_ID = "_legacy_settings"
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

# In-memory caches & locks (process-local).
_INSTALL_TOKEN_LOCK = threading.Lock()
_INSTALL_TOKEN_CACHE: dict[tuple[str, int], tuple[str, float]] = {}

_REFRESH_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[str, threading.Lock] = {}

_PENDING_STATES_LOCK = threading.Lock()
_PENDING_STATES: dict[str, dict[str, Any]] = {}


# ----- errors --------------------------------------------------------


class GithubAcceptedError(Exception):
    """GitHub returned 202 Accepted: endpoint is async and stats are still
    being computed (e.g. ``/stats/commit_activity``). Widgets should avoid
    caching the empty result so the next render picks up computed data."""


class GithubCoreError(RuntimeError):
    """Structured error raised by ``github_core`` so widgets can show clear,
    actionable diagnostics in the UI without leaking secrets."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status_code: int | None = None,
        account_name: str = "",
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.account_name = account_name
        self.hint = hint


# ----- HTTP body decoding & helpers ----------------------------------


def _decode_body(resp: Any) -> bytes:
    if _tesserae_decode_body is not None:
        return bytes(_tesserae_decode_body(resp))
    raw = resp.read()
    enc = ""
    hdrs = getattr(resp, "headers", None)
    if hdrs is not None and hasattr(hdrs, "get"):
        enc = str(hdrs.get("Content-Encoding") or "").strip().lower()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw)
    return bytes(raw)


def _header_get(headers_obj: Any, name: str) -> str:
    if headers_obj is None:
        return ""
    if hasattr(headers_obj, "get"):
        val = headers_obj.get(name)
        if val is not None:
            return str(val).strip()
    if isinstance(headers_obj, dict):
        target = name.lower()
        for k, v in headers_obj.items():
            if str(k).lower() == target and v is not None:
                return str(v).strip()
    return ""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_iso_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def _slugify(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return base or "account"


def _unique_id(items: list[dict[str, Any]], base: str) -> str:
    taken = {str(a.get("id") or "") for a in items if isinstance(a, dict)}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _refresh_lock_for(account_id: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[account_id] = lock
        return lock


def mask_secret(value: str) -> str:
    """Return a masked display representation of a token or secret."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "••••"
    if text.startswith("github_pat_") and len(text) > 15:
        return f"github_pat_••••{text[-4:]}"
    if text.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_")):
        return f"{text[:4]}••••{text[-4:]}"
    return f"••••{text[-4:]}"


# ----- storage & encryption ------------------------------------------


def _data_dir() -> Path:
    registry = current_app.config["PLUGIN_REGISTRY"]
    plugin = registry.get("github_core")
    if plugin is None:
        raise RuntimeError("github_core plugin not registered")
    return Path(plugin.data_dir)


def _secret_box() -> Any:
    if not has_app_context():
        return None
    box = current_app.config.get("SECRET_BOX")
    if SecretBox is not None and isinstance(box, SecretBox):
        return box
    if box is not None and hasattr(box, "wrap") and hasattr(box, "unwrap"):
        return box
    raise RuntimeError("SecretBox is not configured")


def _wrap_secret(plaintext: str, box: Any = None) -> str:
    if not plaintext:
        return ""
    resolved = box if box is not None else _secret_box()
    if resolved is None:
        return plaintext
    return str(resolved.wrap(plaintext))


def _unwrap_secret(stored: str, box: Any = None) -> str:
    if not stored:
        return ""
    resolved = box if box is not None else _secret_box()
    if resolved is None:
        return stored
    return str(resolved.unwrap(stored))


def _store_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "accounts.json"


def _load_raw(data_dir: Path | None = None) -> dict[str, Any]:
    path = _store_path(data_dir)
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "apps": [], "accounts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": _SCHEMA_VERSION, "apps": [], "accounts": []}
    if not isinstance(data, dict):
        return {"version": _SCHEMA_VERSION, "apps": [], "accounts": []}
    apps = data.get("apps")
    accounts = data.get("accounts")
    return {
        "version": _SCHEMA_VERSION,
        "apps": [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else [],
        "accounts": [a for a in accounts if isinstance(a, dict)]
        if isinstance(accounts, list)
        else [],
    }


def _save_raw(data: dict[str, Any], data_dir: Path | None = None) -> None:
    path = _store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _SCHEMA_VERSION,
        "apps": data.get("apps", []),
        "accounts": data.get("accounts", []),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _legacy_settings() -> dict[str, Any]:
    """Read legacy single-token settings from ``SETTINGS_STORE`` if present."""
    if not has_app_context():
        return {}
    store = current_app.config.get("SETTINGS_STORE")
    if store is None:
        return {}
    try:
        section = store.get_section("plugins") or {}
    except Exception:
        return {}
    raw = section.get("github_core")
    if not isinstance(raw, dict):
        return {}
    token_raw = str(raw.get("token_secret") or raw.get("token") or "").strip()
    token = ""
    if token_raw:
        with contextlib.suppress(Exception):
            token = _unwrap_secret(token_raw).strip()
    username = str(raw.get("username") or "").strip()
    if not token and not username:
        return {}
    return {"token": token, "username": username}


def _public_base_url() -> str:
    """Resolve the browser-reachable base URL of this Tesserae instance."""
    if has_app_context():
        store = current_app.config.get("SETTINGS_STORE")
        if store is not None:
            with contextlib.suppress(Exception):
                app_sec = store.get_section("app") or {}
                pub = str(app_sec.get("public_url") or "").strip().rstrip("/")
                if pub.startswith(("http://", "https://")):
                    return pub
    try:
        return request.url_root.rstrip("/")
    except RuntimeError:
        return "http://localhost:5000"


# ----- GitHub App JWT (RS256 via cryptography) -----------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def validate_private_key_pem(pem_text: str) -> rsa.RSAPrivateKey:
    """Parse and validate a GitHub App RSA private key PEM string."""
    raw = (pem_text or "").strip().encode("utf-8")
    if not raw:
        raise ValueError("GitHub App private key (PEM) is required.")
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except Exception as err:
        raise ValueError(f"Invalid PEM private key: {err}") from err
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("GitHub App private key must be an RSA private key.")
    return key


def build_app_jwt(app_id: str, pem_text: str, *, now: float | None = None) -> str:
    """Create a short-lived RS256 JWT for GitHub App authentication."""
    clean_id = str(app_id or "").strip()
    if not clean_id:
        raise GithubCoreError("GitHub App ID is missing.", code="app_jwt_error")
    try:
        private_key = validate_private_key_pem(pem_text)
    except ValueError as err:
        raise GithubCoreError(str(err), code="app_jwt_error") from err

    now_i = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "iat": now_i - 60,
                "exp": now_i + 540,
                "iss": clean_id,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


# ----- pending OAuth / Manifest state store --------------------------


def _prune_states(now: float) -> None:
    expired = [k for k, v in _PENDING_STATES.items() if now - v.get("created", 0.0) > _STATE_TTL_S]
    for k in expired:
        _PENDING_STATES.pop(k, None)


def _create_state(kind: str, payload: dict[str, Any]) -> str:
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _PENDING_STATES_LOCK:
        _prune_states(now)
        _PENDING_STATES[state] = {"kind": kind, "created": now, **payload}
    return state


def _consume_state(state: str, *, expected_kind: str) -> dict[str, Any] | None:
    clean = (state or "").strip()
    if not clean:
        return None
    now = time.time()
    with _PENDING_STATES_LOCK:
        _prune_states(now)
        entry = _PENDING_STATES.get(clean)
        if entry is None or entry.get("kind") != expected_kind:
            return None
        return _PENDING_STATES.pop(clean, None)


# ----- public serialization helpers ----------------------------------


def _pat_kind(token: str) -> str:
    clean = (token or "").strip()
    if clean.startswith("github_pat_"):
        return "Fine-grained"
    if clean.startswith("ghp_"):
        return "Classic"
    return ""


def _app_public(
    raw: dict[str, Any],
    box: Any = None,
    *,
    account_count: int = 0,
) -> dict[str, Any]:
    unreadable = False
    for key in ("pem_secret", "client_secret_secret"):
        stored = str(raw.get(key) or "")
        if stored:
            try:
                _unwrap_secret(stored, box)
            except SecretBoxError:
                unreadable = True
    slug = str(raw.get("slug") or "")
    install_url = f"{GITHUB_WEB_BASE}/apps/{slug}/installations/new" if slug else ""
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "slug": slug,
        "html_url": str(raw.get("html_url") or ""),
        "install_url": install_url,
        "app_id": str(raw.get("app_id") or ""),
        "client_id": str(raw.get("client_id") or ""),
        "has_oauth": bool(raw.get("client_id") and raw.get("client_secret_secret")),
        "has_pem": bool(raw.get("pem_secret")),
        "owner": str(raw.get("owner") or ""),
        "account_count": account_count,
        "secret_unreadable": unreadable,
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "last_synced_at": raw.get("last_synced_at"),
        "last_error": raw.get("last_error"),
    }


def _auth_type_label(auth_type: str, account_type: str = "") -> str:
    if auth_type == "github_app_user":
        return "GitHub App · User"
    if auth_type == "github_app_installation":
        if account_type.lower() == "organization":
            return "GitHub App · Org"
        return "GitHub App · Installation"
    return "PAT"


def _account_public(raw: dict[str, Any], apps_by_id: dict[str, dict[str, Any]], box: Any = None) -> dict[str, Any]:
    auth_type = str(raw.get("auth_type") or "pat")
    account_type = str(raw.get("account_type") or "User")
    masked = ""
    pat_kind = ""
    unreadable = False

    if auth_type == "pat":
        stored = str(raw.get("pat_secret") or "")
        if stored:
            try:
                plaintext = _unwrap_secret(stored, box)
                masked = mask_secret(plaintext)
                pat_kind = _pat_kind(plaintext)
            except SecretBoxError:
                unreadable = True
    elif auth_type == "github_app_user":
        stored = str(raw.get("access_token_secret") or "")
        if stored:
            try:
                masked = mask_secret(_unwrap_secret(stored, box))
            except SecretBoxError:
                unreadable = True
        refresh_stored = str(raw.get("refresh_token_secret") or "")
        if refresh_stored:
            try:
                _unwrap_secret(refresh_stored, box)
            except SecretBoxError:
                unreadable = True
    elif auth_type == "github_app_installation":
        inst_id = raw.get("installation_id")
        masked = f"installation #{inst_id}" if inst_id else ""

    app_ref = str(raw.get("app_ref") or "")
    app_rec = apps_by_id.get(app_ref)
    if auth_type.startswith("github_app_") and app_rec is None:
        unreadable = True

    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "auth_type": auth_type,
        "auth_label": _auth_type_label(auth_type, account_type),
        "pat_kind": pat_kind,
        "account_type": account_type,
        "username": str(raw.get("username") or ""),
        "app_ref": app_ref,
        "app_name": str((app_rec or {}).get("name") or ""),
        "installation_id": raw.get("installation_id"),
        "repositories_selection": str(raw.get("repositories_selection") or ""),
        "masked_secret": masked,
        "secret_unreadable": unreadable,
        "has_refresh_token": bool(raw.get("refresh_token_secret")),
        "token_expires_at": raw.get("token_expires_at"),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "last_tested_at": raw.get("last_tested_at"),
        "last_error": raw.get("last_error"),
    }


# ----- GitHub App CRUD & flows ---------------------------------------


def list_apps(*, data_dir: Path | None = None) -> list[dict[str, Any]]:
    box = _secret_box()
    data = _load_raw(data_dir)
    counts: dict[str, int] = {}
    for acc in data["accounts"]:
        ref = str(acc.get("app_ref") or "")
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    return [_app_public(a, box, account_count=counts.get(str(a.get("id") or ""), 0)) for a in data["apps"]]


def _get_app_secrets(app_ref: str, *, data_dir: Path | None = None) -> dict[str, Any]:
    data = _load_raw(data_dir)
    rec = next((a for a in data["apps"] if str(a.get("id") or "") == app_ref), None)
    if rec is None:
        raise GithubCoreError(
            f"GitHub App '{app_ref}' was not found. Reconnect it in GitHub Core (/plugins/github_core/).",
            code="app_not_found",
        )
    box = _secret_box()
    try:
        pem = _unwrap_secret(str(rec.get("pem_secret") or ""), box)
        client_secret = _unwrap_secret(str(rec.get("client_secret_secret") or ""), box)
    except SecretBoxError as err:
        raise GithubCoreError(
            f"GitHub App '{rec.get('name') or app_ref}' credentials can no longer be decrypted "
            "(the encryption key changed). Re-register the app in GitHub Core (/plugins/github_core/).",
            code="decrypt_error",
        ) from err
    return {
        "id": str(rec.get("id") or ""),
        "name": str(rec.get("name") or ""),
        "slug": str(rec.get("slug") or ""),
        "html_url": str(rec.get("html_url") or ""),
        "app_id": str(rec.get("app_id") or ""),
        "client_id": str(rec.get("client_id") or ""),
        "client_secret": client_secret,
        "pem": pem,
        "owner": str(rec.get("owner") or ""),
    }


def build_app_manifest(name: str, base_url: str) -> dict[str, Any]:
    """Build the GitHub App Manifest JSON for 1-click app creation."""
    clean_base = base_url.rstrip("/")
    clean_name = (name or "").strip() or "Tesserae GitHub"
    return {
        "name": clean_name[:34],
        "url": clean_base,
        "callback_urls": [f"{clean_base}/plugins/github_core/oauth/callback"],
        "setup_url": f"{clean_base}/plugins/github_core/apps/installed",
        "redirect_url": f"{clean_base}/plugins/github_core/apps/manifest/callback",
        "request_oauth_on_install": True,
        "setup_on_update": True,
        "public": False,
        "default_permissions": {
            "actions": "read",
            "contents": "read",
            "discussions": "read",
            "issues": "read",
            "metadata": "read",
            "pull_requests": "read",
        },
        "default_events": [],
    }


def _upsert_app(
    *,
    name: str,
    app_id: str,
    pem: str,
    client_id: str = "",
    client_secret: str = "",
    slug: str = "",
    html_url: str = "",
    owner: str = "",
    data_dir: Path | None = None,
) -> dict[str, Any]:
    clean_name = (name or "").strip()
    clean_app_id = str(app_id or "").strip()
    clean_pem = (pem or "").strip()
    if not clean_name:
        raise ValueError("GitHub App name is required.")
    if not clean_app_id or not clean_app_id.isdigit():
        raise ValueError("GitHub App ID must be numeric.")
    validate_private_key_pem(clean_pem)

    box = _secret_box()
    data = _load_raw(data_dir)
    now = _now_iso()
    existing = next((a for a in data["apps"] if str(a.get("app_id") or "") == clean_app_id), None)
    if existing is not None:
        existing["name"] = clean_name
        existing["pem_secret"] = _wrap_secret(clean_pem, box)
        if client_id.strip():
            existing["client_id"] = client_id.strip()
        if client_secret.strip():
            existing["client_secret_secret"] = _wrap_secret(client_secret.strip(), box)
        if slug.strip():
            existing["slug"] = slug.strip()
        if html_url.strip():
            existing["html_url"] = html_url.strip()
        if owner.strip():
            existing["owner"] = owner.strip()
        existing["updated_at"] = now
        existing["last_error"] = None
        record = existing
    else:
        record = {
            "id": _unique_id(data["apps"], _slugify(slug or clean_name)),
            "name": clean_name,
            "slug": slug.strip(),
            "html_url": html_url.strip(),
            "app_id": clean_app_id,
            "client_id": client_id.strip(),
            "client_secret_secret": _wrap_secret(client_secret.strip(), box)
            if client_secret.strip()
            else "",
            "pem_secret": _wrap_secret(clean_pem, box),
            "owner": owner.strip(),
            "created_at": now,
            "updated_at": now,
            "last_synced_at": None,
            "last_error": None,
        }
        data["apps"].append(record)

    _save_raw(data, data_dir)
    return _app_public(record, box)


def convert_manifest_code(code: str, *, data_dir: Path | None = None) -> dict[str, Any]:
    """Exchange a temporary GitHub App Manifest ``code`` for full App credentials."""
    clean_code = (code or "").strip()
    if not clean_code:
        raise GithubCoreError("Missing manifest conversion code from GitHub.", code="manifest_error")
    url = f"{GITHUB_API_BASE}/app-manifests/{urllib.parse.quote(clean_code, safe='')}/conversions"
    req = urllib.request.Request(
        url,
        data=b"",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(_decode_body(resp).decode("utf-8"))
    except Exception as err:
        raise GithubCoreError(
            coerce_error(err, endpoint="GitHub App Manifest conversion"),
            code="manifest_error",
        ) from err

    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("pem"):
        raise GithubCoreError(
            "GitHub returned an incomplete App Manifest conversion response.",
            code="manifest_error",
        )
    owner_obj = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    return _upsert_app(
        name=str(payload.get("name") or "Tesserae GitHub"),
        app_id=str(payload["id"]),
        pem=str(payload["pem"]),
        client_id=str(payload.get("client_id") or ""),
        client_secret=str(payload.get("client_secret") or ""),
        slug=str(payload.get("slug") or ""),
        html_url=str(payload.get("html_url") or ""),
        owner=str(owner_obj.get("login") or ""),
        data_dir=data_dir,
    )


def _probe_app_metadata(app_id: str, pem: str) -> dict[str, str]:
    """Best-effort ``GET /app`` probe using an App JWT to fetch slug/url/owner."""
    out = {"slug": "", "html_url": "", "owner": "", "name": ""}
    with contextlib.suppress(Exception):
        app_jwt = build_app_jwt(app_id, pem)
        req = urllib.request.Request(
            f"{GITHUB_API_BASE}/app",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {app_jwt}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            meta = json.loads(_decode_body(resp).decode("utf-8"))
        if isinstance(meta, dict):
            out["slug"] = str(meta.get("slug") or "")
            out["html_url"] = str(meta.get("html_url") or "")
            if meta.get("name"):
                out["name"] = str(meta["name"])
            if isinstance(meta.get("owner"), dict):
                out["owner"] = str(meta["owner"].get("login") or "")
    return out


def create_app_manual(
    *,
    name: str,
    app_id: str,
    pem: str,
    client_id: str = "",
    client_secret: str = "",
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Register an existing GitHub App manually and probe ``GET /app`` for metadata."""
    clean_name = (name or "").strip()
    clean_app_id = str(app_id or "").strip()
    clean_pem = (pem or "").strip()
    if not clean_app_id or not clean_app_id.isdigit():
        raise ValueError("GitHub App ID must be numeric.")
    validate_private_key_pem(clean_pem)

    meta = _probe_app_metadata(clean_app_id, clean_pem)
    if not clean_name and meta["name"]:
        clean_name = meta["name"]

    return _upsert_app(
        name=clean_name or f"GitHub App {clean_app_id}",
        app_id=clean_app_id,
        pem=clean_pem,
        client_id=client_id,
        client_secret=client_secret,
        slug=meta["slug"],
        html_url=meta["html_url"],
        owner=meta["owner"],
        data_dir=data_dir,
    )


def create_apps_manual_batch(
    entries: list[dict[str, Any]],
    *,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Register one or multiple existing GitHub Apps manually in a single batch."""
    created: list[dict[str, Any]] = []
    errors: list[str] = []
    populated = 0

    for idx, entry in enumerate(entries, start=1):
        name = str(entry.get("name") or "").strip()
        app_id = str(entry.get("app_id") or "").strip()
        client_id = str(entry.get("client_id") or "").strip()
        client_secret = str(entry.get("client_secret") or "").strip()
        pem = str(entry.get("pem") or "").strip()
        if not any((name, app_id, client_id, client_secret, pem)):
            continue
        populated += 1
        try:
            app = create_app_manual(
                name=name,
                app_id=app_id,
                pem=pem,
                client_id=client_id,
                client_secret=client_secret,
                data_dir=data_dir,
            )
            created.append(app)
        except (ValueError, GithubCoreError) as err:
            label = name or (f"App ID {app_id}" if app_id else f"App #{idx}")
            errors.append(f"{label}: {err}")

    if populated == 0:
        errors.append("Enter at least one GitHub App to register.")
    return {"created": created, "errors": errors}


def update_app(
    app_ref: str,
    *,
    name: str | None = None,
    app_id: str | None = None,
    pem: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Update a registered GitHub App's name, ID, OAuth credentials, or PEM key."""
    box = _secret_box()
    data = _load_raw(data_dir)
    record = next((a for a in data["apps"] if str(a.get("id") or "") == app_ref), None)
    if record is None:
        return None

    creds_changed = False
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("GitHub App name is required.")
        record["name"] = clean_name

    if app_id is not None and app_id.strip():
        clean_app_id = app_id.strip()
        if not clean_app_id.isdigit():
            raise ValueError("GitHub App ID must be numeric.")
        duplicate = next(
            (
                a
                for a in data["apps"]
                if str(a.get("id") or "") != app_ref and str(a.get("app_id") or "") == clean_app_id
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError(
                f"GitHub App ID {clean_app_id} is already registered under '{duplicate.get('name') or duplicate.get('id')}'."
            )
        if clean_app_id != str(record.get("app_id") or ""):
            record["app_id"] = clean_app_id
            creds_changed = True

    if client_id is not None:
        record["client_id"] = client_id.strip()

    if client_secret is not None and client_secret.strip():
        record["client_secret_secret"] = _wrap_secret(client_secret.strip(), box)

    new_pem = (pem or "").strip() if pem is not None else ""
    if new_pem:
        validate_private_key_pem(new_pem)
        record["pem_secret"] = _wrap_secret(new_pem, box)
        creds_changed = True

    if creds_changed:
        effective_pem = new_pem
        if not effective_pem:
            with contextlib.suppress(SecretBoxError):
                effective_pem = _unwrap_secret(str(record.get("pem_secret") or ""), box)
        if effective_pem:
            meta = _probe_app_metadata(str(record.get("app_id") or ""), effective_pem)
            if meta["slug"]:
                record["slug"] = meta["slug"]
            if meta["html_url"]:
                record["html_url"] = meta["html_url"]
            if meta["owner"]:
                record["owner"] = meta["owner"]
        with _INSTALL_TOKEN_LOCK:
            for key in [k for k in _INSTALL_TOKEN_CACHE if k[0] == app_ref]:
                _INSTALL_TOKEN_CACHE.pop(key, None)

    record["updated_at"] = _now_iso()
    record["last_error"] = None
    _save_raw(data, data_dir)
    count = sum(1 for acc in data["accounts"] if str(acc.get("app_ref") or "") == app_ref)
    return _app_public(record, box, account_count=count)


def test_app(app_ref: str, *, data_dir: Path | None = None) -> dict[str, Any]:
    """Verify a GitHub App's App ID + PEM private key via ``GET /app``."""
    try:
        app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
    except GithubCoreError as err:
        return {"ok": False, "slug": None, "owner": None, "detail": None, "error": err.message}

    try:
        meta = _app_jwt_request(app_secrets, f"{GITHUB_API_BASE}/app", method="GET")
    except Exception as err:
        msg = coerce_error(err, account_name=app_secrets["name"], endpoint="GET /app")
        data = _load_raw(data_dir)
        for a in data["apps"]:
            if str(a.get("id") or "") == app_ref:
                a["last_error"] = msg
        _save_raw(data, data_dir)
        return {"ok": False, "slug": None, "owner": None, "detail": None, "error": msg}

    slug = str((meta or {}).get("slug") or app_secrets["slug"])
    html_url = str((meta or {}).get("html_url") or app_secrets["html_url"])
    owner_obj = (meta or {}).get("owner") if isinstance((meta or {}).get("owner"), dict) else {}
    owner = str(owner_obj.get("login") or app_secrets["owner"])
    installs = (meta or {}).get("installations_count")

    data = _load_raw(data_dir)
    now = _now_iso()
    for a in data["apps"]:
        if str(a.get("id") or "") == app_ref:
            if slug:
                a["slug"] = slug
            if html_url:
                a["html_url"] = html_url
            if owner:
                a["owner"] = owner
            a["updated_at"] = now
            a["last_error"] = None
    _save_raw(data, data_dir)

    parts: list[str] = []
    if slug:
        parts.append(slug)
    if owner:
        parts.append(f"owner @{owner}")
    if isinstance(installs, int):
        parts.append(f"{installs} installation{'s' if installs != 1 else ''}")
    return {
        "ok": True,
        "slug": slug or None,
        "owner": owner or None,
        "detail": " · ".join(parts) or "OK",
        "error": None,
    }


def delete_app(app_ref: str, *, data_dir: Path | None = None) -> bool:
    """Delete a registered GitHub App and all accounts bound to it."""
    data = _load_raw(data_dir)
    before = len(data["apps"])
    data["apps"] = [a for a in data["apps"] if str(a.get("id") or "") != app_ref]
    if len(data["apps"]) == before:
        return False
    # Remove installation and OAuth user accounts tied to this app (they depend on its credentials).
    data["accounts"] = [
        a
        for a in data["accounts"]
        if not (
            str(a.get("app_ref") or "") == app_ref
            and str(a.get("auth_type") or "").startswith("github_app_")
        )
    ]
    with _INSTALL_TOKEN_LOCK:
        for key in [k for k in _INSTALL_TOKEN_CACHE if k[0] == app_ref]:
            _INSTALL_TOKEN_CACHE.pop(key, None)
    _save_raw(data, data_dir)
    return True


def _app_jwt_request(
    app_secrets: dict[str, Any],
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = HTTP_TIMEOUT_S,
) -> Any:
    app_jwt = build_app_jwt(app_secrets["app_id"], app_secrets["pem"])
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": API_VERSION,
        "Authorization": f"Bearer {app_jwt}",
        "Accept-Encoding": "identity",
    }
    raw_body: bytes | None = None
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif method == "POST":
        raw_body = b""
    req = urllib.request.Request(url, data=raw_body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = _decode_body(resp).decode("utf-8")
        return json.loads(text) if text.strip() else {}


def sync_app_installations(app_ref: str, *, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Discover all personal & organization installations for ``app_ref`` via
    ``GET /app/installations`` and upsert them as selectable accounts."""
    app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
    installations: list[dict[str, Any]] = []
    for page in range(1, 6):
        url = f"{GITHUB_API_BASE}/app/installations?per_page=100&page={page}"
        try:
            batch = _app_jwt_request(app_secrets, url, method="GET")
        except Exception as err:
            msg = coerce_error(err, account_name=app_secrets["name"], endpoint="GET /app/installations")
            data = _load_raw(data_dir)
            for a in data["apps"]:
                if a.get("id") == app_ref:
                    a["last_error"] = msg
            _save_raw(data, data_dir)
            raise GithubCoreError(msg, code="sync_failed", account_name=app_secrets["name"]) from err
        if not isinstance(batch, list) or not batch:
            break
        installations.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break

    data = _load_raw(data_dir)
    now = _now_iso()
    seen_inst_ids: set[int] = set()
    synced: list[dict[str, Any]] = []

    for inst in installations:
        inst_id = inst.get("id")
        if not isinstance(inst_id, int):
            continue
        seen_inst_ids.add(inst_id)
        acc_obj = inst.get("account") if isinstance(inst.get("account"), dict) else {}
        login = str(acc_obj.get("login") or "").strip()
        acc_type = str(acc_obj.get("type") or "User").strip()
        repo_sel = str(inst.get("repository_selection") or "")
        default_name = f"{login} (Org)" if acc_type.lower() == "organization" else f"{login} (App)"
        taken_names = {str(a.get("name") or "").lower() for a in data["accounts"]}
        if login and default_name.lower() in taken_names:
            default_name = f"{login} ({app_secrets['name']})"

        existing = next(
            (
                a
                for a in data["accounts"]
                if a.get("auth_type") == "github_app_installation"
                and a.get("app_ref") == app_ref
                and a.get("installation_id") == inst_id
            ),
            None,
        )
        if existing is not None:
            existing["username"] = login or existing.get("username") or ""
            existing["account_type"] = acc_type
            existing["repositories_selection"] = repo_sel
            existing["updated_at"] = now
            existing["last_error"] = None
            synced.append(existing)
        else:
            base_slug = _slugify(f"{login or inst_id}-org" if acc_type.lower() == "organization" else f"{login or inst_id}-app")
            rec = {
                "id": _unique_id(data["accounts"], base_slug),
                "name": default_name if login else f"{app_secrets['name']} #{inst_id}",
                "auth_type": "github_app_installation",
                "app_ref": app_ref,
                "username": login,
                "account_type": acc_type,
                "installation_id": inst_id,
                "repositories_selection": repo_sel,
                "created_at": now,
                "updated_at": now,
                "last_tested_at": now,
                "last_error": None,
            }
            data["accounts"].append(rec)
            synced.append(rec)

    # Prune uninstalled installations for this app.
    data["accounts"] = [
        a
        for a in data["accounts"]
        if not (
            a.get("auth_type") == "github_app_installation"
            and a.get("app_ref") == app_ref
            and a.get("installation_id") not in seen_inst_ids
        )
    ]

    for a in data["apps"]:
        if a.get("id") == app_ref:
            a["last_synced_at"] = now
            a["last_error"] = None
    _save_raw(data, data_dir)

    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    box = _secret_box()
    return [_account_public(r, apps_by_id, box) for r in synced]


def sync_all_apps(*, data_dir: Path | None = None) -> dict[str, Any]:
    """Sync installations across every registered GitHub App."""
    data = _load_raw(data_dir)
    synced_apps = 0
    total_installations = 0
    errors: list[str] = []
    for app_rec in data["apps"]:
        app_ref = str(app_rec.get("id") or "")
        app_name = str(app_rec.get("name") or app_ref)
        try:
            items = sync_app_installations(app_ref, data_dir=data_dir)
            synced_apps += 1
            total_installations += len(items)
        except GithubCoreError as err:
            errors.append(f"{app_name}: {err.message}")
    return {
        "synced_apps": synced_apps,
        "total_installations": total_installations,
        "errors": errors,
    }


def mint_installation_token(
    app_ref: str,
    installation_id: int,
    *,
    force_refresh: bool = False,
    data_dir: Path | None = None,
) -> str:
    """Mint (or return cached) 1-hour GitHub App Installation Access Token."""
    cache_key = (app_ref, int(installation_id))
    now = time.time()
    if not force_refresh:
        with _INSTALL_TOKEN_LOCK:
            cached = _INSTALL_TOKEN_CACHE.get(cache_key)
            if cached is not None and cached[1] - now > _TOKEN_REFRESH_MARGIN_S:
                return cached[0]

    app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
    url = f"{GITHUB_API_BASE}/app/installations/{int(installation_id)}/access_tokens"
    try:
        payload = _app_jwt_request(app_secrets, url, method="POST")
    except Exception as err:
        raise GithubCoreError(
            coerce_error(
                err,
                account_name=app_secrets["name"],
                endpoint=f"installation #{installation_id} access token",
            ),
            code="installation_token_failed",
            account_name=app_secrets["name"],
        ) from err

    token = str((payload or {}).get("token") or "").strip()
    if not token:
        raise GithubCoreError(
            f"GitHub did not return an installation token for installation #{installation_id}.",
            code="installation_token_failed",
            account_name=app_secrets["name"],
        )
    expires_at = _parse_iso_ts((payload or {}).get("expires_at")) or (now + 3600)
    with _INSTALL_TOKEN_LOCK:
        _INSTALL_TOKEN_CACHE[cache_key] = (token, expires_at)
    return token


# ----- GitHub App User-to-Server OAuth -------------------------------


def build_oauth_authorize_url(app_ref: str, redirect_uri: str, state: str, *, data_dir: Path | None = None) -> str:
    app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
    if not app_secrets["client_id"] or not app_secrets["client_secret"]:
        raise GithubCoreError(
            f"GitHub App '{app_secrets['name']}' is missing Client ID / Client Secret for user OAuth.",
            code="oauth_unconfigured",
            account_name=app_secrets["name"],
        )
    qs = urllib.parse.urlencode(
        {
            "client_id": app_secrets["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{GITHUB_WEB_BASE}/login/oauth/authorize?{qs}"


def _oauth_token_post(params: dict[str, str], *, account_name: str = "") -> dict[str, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        f"{GITHUB_WEB_BASE}/login/oauth/access_token",
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(_decode_body(resp).decode("utf-8"))
    except Exception as err:
        raise GithubCoreError(
            coerce_error(err, account_name=account_name, endpoint="GitHub OAuth token exchange"),
            code="oauth_exchange_failed",
            account_name=account_name,
        ) from err
    if not isinstance(payload, dict):
        raise GithubCoreError("Unexpected response from GitHub OAuth.", code="oauth_exchange_failed")
    if payload.get("error"):
        desc = payload.get("error_description") or payload.get("error")
        raise GithubCoreError(
            f"GitHub OAuth failed: {desc}",
            code="oauth_exchange_failed",
            account_name=account_name,
        )
    if not payload.get("access_token"):
        raise GithubCoreError(
            "GitHub OAuth did not return an access token.",
            code="oauth_exchange_failed",
            account_name=account_name,
        )
    return payload


def _fetch_viewer_login(token: str) -> str:
    req = urllib.request.Request(
        f"{GITHUB_API_BASE}/user",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        payload = json.loads(_decode_body(resp).decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("login"), str):
        return payload["login"].strip()
    return ""


def exchange_oauth_code(
    app_ref: str,
    code: str,
    redirect_uri: str,
    *,
    installation_id: int | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Exchange a GitHub App OAuth authorization ``code`` and upsert a
    ``github_app_user`` account."""
    app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
    if not app_secrets["client_id"] or not app_secrets["client_secret"]:
        raise GithubCoreError(
            f"GitHub App '{app_secrets['name']}' does not have Client ID and Client Secret configured.",
            code="oauth_unconfigured",
            account_name=app_secrets["name"],
        )
    token_data = _oauth_token_post(
        {
            "client_id": app_secrets["client_id"],
            "client_secret": app_secrets["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
        },
        account_name=app_secrets["name"],
    )
    access_token = str(token_data["access_token"]).strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    now_ts = time.time()
    expires_in = token_data.get("expires_in")
    refresh_expires_in = token_data.get("refresh_token_expires_in")
    token_expires_at = (
        int(now_ts + int(expires_in)) if isinstance(expires_in, (int, float)) else None
    )
    refresh_expires_at = (
        int(now_ts + int(refresh_expires_in))
        if isinstance(refresh_expires_in, (int, float))
        else None
    )

    try:
        username = _fetch_viewer_login(access_token)
    except Exception as err:
        raise GithubCoreError(
            coerce_error(err, account_name=app_secrets["name"], endpoint="GET /user"),
            code="user_lookup_failed",
            account_name=app_secrets["name"],
        ) from err

    box = _secret_box()
    data = _load_raw(data_dir)
    now_iso = _now_iso()

    existing = next(
        (
            a
            for a in data["accounts"]
            if a.get("auth_type") == "github_app_user"
            and a.get("app_ref") == app_ref
            and (username and str(a.get("username") or "").lower() == username.lower())
        ),
        None,
    )
    if existing is not None:
        existing["username"] = username
        existing["access_token_secret"] = _wrap_secret(access_token, box)
        if refresh_token:
            existing["refresh_token_secret"] = _wrap_secret(refresh_token, box)
        existing["token_expires_at"] = token_expires_at
        existing["refresh_token_expires_at"] = refresh_expires_at
        if installation_id is not None:
            existing["installation_id"] = installation_id
        existing["updated_at"] = now_iso
        existing["last_tested_at"] = now_iso
        existing["last_error"] = None
        record = existing
    else:
        default_name = f"@{username}" if username else f"{app_secrets['name']} user"
        taken_names = {str(a.get("name") or "").lower() for a in data["accounts"]}
        if username and default_name.lower() in taken_names:
            default_name = f"@{username} ({app_secrets['name']})"
        record = {
            "id": _unique_id(data["accounts"], _slugify(username or app_secrets["name"])),
            "name": default_name,
            "auth_type": "github_app_user",
            "app_ref": app_ref,
            "username": username,
            "account_type": "User",
            "installation_id": installation_id,
            "access_token_secret": _wrap_secret(access_token, box),
            "refresh_token_secret": _wrap_secret(refresh_token, box) if refresh_token else "",
            "token_expires_at": token_expires_at,
            "refresh_token_expires_at": refresh_expires_at,
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_tested_at": now_iso,
            "last_error": None,
        }
        data["accounts"].append(record)

    _save_raw(data, data_dir)
    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    return _account_public(record, apps_by_id, box)


def refresh_user_token(account_id: str, *, force: bool = False, data_dir: Path | None = None) -> str:
    """Refresh an expiring ``github_app_user`` OAuth token using its refresh token.

    Protected by a per-account lock so concurrent widget fetches never race
    on GitHub's single-use rotated refresh tokens.
    """
    lock = _refresh_lock_for(account_id)
    with lock:
        box = _secret_box()
        data = _load_raw(data_dir)
        record = next((a for a in data["accounts"] if str(a.get("id") or "") == account_id), None)
        if record is None or record.get("auth_type") != "github_app_user":
            raise GithubCoreError(
                f"GitHub Account '{account_id}' not found.",
                code="account_not_found",
            )
        acc_name = str(record.get("name") or account_id)
        try:
            current_access = _unwrap_secret(str(record.get("access_token_secret") or ""), box)
            current_refresh = _unwrap_secret(str(record.get("refresh_token_secret") or ""), box)
        except SecretBoxError as err:
            raise GithubCoreError(
                f"GitHub Account '{acc_name}' token can no longer be decrypted. "
                "Re-authorize it in GitHub Core (/plugins/github_core/).",
                code="decrypt_error",
                account_name=acc_name,
            ) from err

        now_ts = time.time()
        exp = record.get("token_expires_at")
        if (
            not force
            and current_access
            and (exp is None or float(exp) - now_ts > _TOKEN_REFRESH_MARGIN_S)
        ):
            return current_access

        if not current_refresh:
            if not force and current_access:
                return current_access
            raise GithubCoreError(
                f"GitHub OAuth token for '{acc_name}' expired and has no refresh token. "
                "Click 'Authorize user' in GitHub Core (/plugins/github_core/).",
                code="unauthorized",
                account_name=acc_name,
            )

        app_ref = str(record.get("app_ref") or "")
        app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
        token_data = _oauth_token_post(
            {
                "client_id": app_secrets["client_id"],
                "client_secret": app_secrets["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": current_refresh,
            },
            account_name=acc_name,
        )
        new_access = str(token_data["access_token"]).strip()
        new_refresh = str(token_data.get("refresh_token") or current_refresh).strip()
        expires_in = token_data.get("expires_in")
        refresh_expires_in = token_data.get("refresh_token_expires_in")

        record["access_token_secret"] = _wrap_secret(new_access, box)
        record["refresh_token_secret"] = _wrap_secret(new_refresh, box)
        if isinstance(expires_in, (int, float)):
            record["token_expires_at"] = int(now_ts + int(expires_in))
        if isinstance(refresh_expires_in, (int, float)):
            record["refresh_token_expires_at"] = int(now_ts + int(refresh_expires_in))
        record["updated_at"] = _now_iso()
        record["last_error"] = None
        _save_raw(data, data_dir)
        return new_access


def _find_app_for_installation(installation_id: int, *, data_dir: Path | None = None) -> str | None:
    """Locate which saved GitHub App owns ``installation_id`` (for post-install callbacks)."""
    data = _load_raw(data_dir)
    if len(data["apps"]) == 1:
        return str(data["apps"][0].get("id") or "")
    for app_rec in data["apps"]:
        app_ref = str(app_rec.get("id") or "")
        try:
            app_secrets = _get_app_secrets(app_ref, data_dir=data_dir)
            _app_jwt_request(
                app_secrets,
                f"{GITHUB_API_BASE}/app/installations/{int(installation_id)}",
                method="GET",
            )
            return app_ref
        except Exception:
            continue
    return None


# ----- Account CRUD (PAT fallback + rename/delete) -------------------


def list_accounts(*, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return safe metadata for all saved GitHub Accounts (never raw secrets)."""
    box = _secret_box()
    data = _load_raw(data_dir)
    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    return [_account_public(acc, apps_by_id, box) for acc in data["accounts"]]


def create_account(
    name: str,
    pat: str,
    *,
    username: str = "",
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Create and persist a Personal Access Token account."""
    clean_name = (name or "").strip()
    clean_pat = (pat or "").strip()
    clean_user = (username or "").strip().lstrip("@")
    if not clean_name:
        raise ValueError("GitHub Account name is required.")
    if not clean_pat:
        raise ValueError("Personal Access Token is required.")

    box = _secret_box()
    data = _load_raw(data_dir)
    now = _now_iso()
    record: dict[str, Any] = {
        "id": _unique_id(data["accounts"], _slugify(clean_name)),
        "name": clean_name,
        "auth_type": "pat",
        "account_type": "User",
        "pat_secret": _wrap_secret(clean_pat, box),
        "username": clean_user,
        "created_at": now,
        "updated_at": now,
        "last_tested_at": None,
        "last_error": None,
    }
    data["accounts"].append(record)
    _save_raw(data, data_dir)
    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    return _account_public(record, apps_by_id, box)


def create_accounts_batch(
    entries: list[dict[str, Any]],
    *,
    verify: bool = False,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Create one or multiple Personal Access Token accounts in a single batch."""
    created: list[dict[str, Any]] = []
    verified = 0
    errors: list[str] = []
    populated = 0

    for idx, entry in enumerate(entries, start=1):
        name = str(entry.get("name") or "").strip()
        pat = str(entry.get("pat") or "").strip()
        username = str(entry.get("username") or "").strip().lstrip("@")
        if not any((name, pat, username)):
            continue
        populated += 1
        try:
            acc = create_account(name, pat, username=username, data_dir=data_dir)
        except ValueError as err:
            label = name or f"PAT #{idx}"
            errors.append(f"{label}: {err}")
            continue
        if verify:
            res = test_connection(acc["id"], data_dir=data_dir)
            if res["ok"]:
                verified += 1
            elif res.get("error"):
                errors.append(f"{acc['name']} (saved, test failed): {res['error']}")
            refreshed = next(
                (a for a in list_accounts(data_dir=data_dir) if a["id"] == acc["id"]),
                acc,
            )
            created.append(refreshed)
        else:
            created.append(acc)

    if populated == 0:
        errors.append("Enter at least one Personal Access Token.")
    return {"created": created, "verified": verified, "errors": errors}


def update_account(
    account_id: str,
    *,
    name: str | None = None,
    pat: str | None = None,
    username: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Rename a saved account and/or update a PAT account's token or username."""
    box = _secret_box()
    data = _load_raw(data_dir)
    record = next((a for a in data["accounts"] if str(a.get("id") or "") == account_id), None)
    if record is None:
        return None
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("GitHub Account name is required.")
        record["name"] = clean_name
    if pat is not None and pat.strip() and record.get("auth_type", "pat") == "pat":
        record["pat_secret"] = _wrap_secret(pat.strip(), box)
        record["username"] = ""
        record["last_tested_at"] = None
        record["last_error"] = None
    if username is not None and record.get("auth_type", "pat") == "pat":
        clean_user = username.strip().lstrip("@")
        if clean_user or not (pat and pat.strip()):
            record["username"] = clean_user
    record["updated_at"] = _now_iso()
    _save_raw(data, data_dir)
    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    return _account_public(record, apps_by_id, box)


def set_default_account(account_id: str, *, data_dir: Path | None = None) -> dict[str, Any] | None:
    """Move ``account_id`` to the top of the list so widgets use it as Default."""
    box = _secret_box()
    data = _load_raw(data_dir)
    idx = next((i for i, a in enumerate(data["accounts"]) if str(a.get("id") or "") == account_id), None)
    if idx is None:
        return None
    record = data["accounts"].pop(idx)
    data["accounts"].insert(0, record)
    _save_raw(data, data_dir)
    apps_by_id = {str(a.get("id") or ""): a for a in data["apps"]}
    return _account_public(record, apps_by_id, box)


def delete_account(account_id: str, *, data_dir: Path | None = None) -> bool:
    data = _load_raw(data_dir)
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if str(a.get("id") or "") != account_id]
    if len(data["accounts"]) == before:
        return False
    _save_raw(data, data_dir)
    return True


# ----- Account resolution & live token retrieval ---------------------


def _resolve_live_token(
    record: dict[str, Any],
    *,
    force_refresh: bool = False,
    data_dir: Path | None = None,
) -> str:
    auth_type = str(record.get("auth_type") or "pat")
    acc_id = str(record.get("id") or "")
    acc_name = str(record.get("name") or acc_id)

    if auth_type == "github_app_user":
        return refresh_user_token(acc_id, force=force_refresh, data_dir=data_dir)

    if auth_type == "github_app_installation":
        app_ref = str(record.get("app_ref") or "")
        inst_id = record.get("installation_id")
        if not app_ref or not isinstance(inst_id, int):
            raise GithubCoreError(
                f"GitHub App installation '{acc_name}' is missing its installation ID. "
                "Re-sync installations in GitHub Core (/plugins/github_core/).",
                code="installation_invalid",
                account_name=acc_name,
            )
        return mint_installation_token(
            app_ref, inst_id, force_refresh=force_refresh, data_dir=data_dir
        )

    # Fallback: PAT
    box = _secret_box()
    stored = str(record.get("pat_secret") or "")
    if not stored:
        return ""
    try:
        return _unwrap_secret(stored, box).strip()
    except SecretBoxError as err:
        raise GithubCoreError(
            f"GitHub Account '{acc_name}' token can no longer be decrypted (the encryption key "
            "changed). Re-enter it in GitHub Core (/plugins/github_core/).",
            code="decrypt_error",
            account_name=acc_name,
        ) from err


def get_account(
    account_id: str | None = None,
    *,
    force_refresh: bool = False,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return one GitHub Account with its live bearer token for server-side use.

    When ``account_id`` is blank or ``None``, defaults to the first saved
    account (preferring a user-authorized account if one exists? No: first
    saved account in list order, predictable and matches the UI). Falls back
    to legacy ``SETTINGS_STORE`` credentials if no accounts exist yet.
    """
    data = _load_raw(data_dir)
    accounts = data["accounts"]
    wanted = (account_id or "").strip()

    if not accounts:
        legacy = _legacy_settings()
        if not wanted and legacy:
            return {
                "id": _LEGACY_ACCOUNT_ID,
                "name": "Legacy GitHub Core settings",
                "auth_type": "pat",
                "account_type": "User",
                "username": legacy["username"],
                "token": legacy["token"],
                "pat": legacy["token"],
                "installation_id": None,
                "app_ref": "",
            }
        return None

    if not wanted:
        record = accounts[0]
    else:
        record = next((a for a in accounts if str(a.get("id") or "") == wanted), None)
        if record is None:
            return None

    token = _resolve_live_token(record, force_refresh=force_refresh, data_dir=data_dir)
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or record.get("id") or ""),
        "auth_type": str(record.get("auth_type") or "pat"),
        "account_type": str(record.get("account_type") or "User"),
        "username": str(record.get("username") or ""),
        "token": token,
        "pat": token,  # backwards-compatible alias
        "installation_id": record.get("installation_id"),
        "app_ref": str(record.get("app_ref") or ""),
    }


def resolve_account(
    account_id: str | None = None,
    *,
    require_auth: bool = False,
    require_user_token: bool = False,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Validate and resolve the selected account for a widget's ``fetch()``.

    Raises ``GithubCoreError`` with an actionable UI message when:
      * an explicit ``account_id`` was selected but no longer exists;
      * ``require_auth=True`` and no account/token is configured;
      * ``require_user_token=True`` and the chosen account is an org/installation
        account rather than a user-authorized account.
    """
    wanted = (account_id or "").strip()
    account = get_account(wanted or None, data_dir=data_dir)

    if account is None:
        if wanted:
            raise GithubCoreError(
                f"GitHub account '{wanted}' was not found. "
                "Pick another account in widget options or reconnect it in GitHub Core (/plugins/github_core/).",
                code="account_not_found",
            )
        if require_auth or require_user_token:
            raise GithubCoreError(
                "No GitHub account connected. Connect a GitHub App in GitHub Core (/plugins/github_core/).",
                code="no_account",
            )
        return None

    if (require_auth or require_user_token) and not account.get("token"):
        raise GithubCoreError(
            f"GitHub account '{account['name']}' has no active token. "
            "Reconnect or test it in GitHub Core (/plugins/github_core/).",
            code="auth_required",
            account_name=account["name"],
        )

    if require_user_token and account.get("auth_type") == "github_app_installation":
        raise GithubCoreError(
            f"Account '{account['name']}' is a GitHub App installation ({account['account_type']}), "
            "which cannot query user-scoped endpoints. Authorize a user on this GitHub App in "
            "GitHub Core (/plugins/github_core/) or select a user account.",
            code="user_token_required",
            account_name=account["name"],
        )

    return account


def account_choices(
    *,
    require_auth: bool = False,
    data_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Dropdown choices for ``choices_from: "account"`` in all ``github_*`` widgets."""
    accounts = list_accounts(data_dir=data_dir)
    if not accounts:
        legacy = _legacy_settings()
        if legacy.get("token"):
            label = (
                f"Default (@{legacy['username']}, legacy settings)"
                if legacy.get("username")
                else "Default (legacy settings)"
            )
            return [{"value": "", "label": label}]
        if require_auth:
            return [
                {
                    "value": "",
                    "label": "Connect a GitHub App or account in GitHub Core (/plugins/github_core/)",
                    "note": True,
                }
            ]
        return [{"value": "", "label": "Public (no account connected)"}]

    apps = list_apps(data_dir=data_dir)
    multi_app = len(apps) > 1

    def _fmt(acc: dict[str, Any]) -> str:
        name = acc["name"]
        user = acc["username"]
        kind = acc["auth_label"]
        app_name = acc.get("app_name") or ""
        if user and f"@{user}" not in name and user.lower() not in name.lower():
            base = f"{name} (@{user})"
        else:
            base = name
        if multi_app and app_name and app_name.lower() not in base.lower():
            return f"{base} · {kind} · {app_name}"
        return f"{base} · {kind}"

    out: list[dict[str, Any]] = [
        {"value": "", "label": f"Default ({_fmt(accounts[0])})"},
    ]
    for acc in accounts:
        out.append({"value": acc["id"], "label": _fmt(acc)})
    return out


# ----- backwards-compatible + multi-account HTTP helpers -------------


def get_token(account_id: str | None = None, *, data_dir: Path | None = None) -> str:
    """Return the active bearer token for ``account_id`` (or default), or ``""``."""
    try:
        acc = get_account(account_id, data_dir=data_dir)
    except GithubCoreError:
        return ""
    return str((acc or {}).get("token") or "").strip()


def get_username(account_id: str | None = None, *, data_dir: Path | None = None) -> str:
    """Return the configured username/org login for ``account_id`` (or default)."""
    data = _load_raw(data_dir)
    accounts = data["accounts"]
    wanted = (account_id or "").strip()
    if not accounts:
        return str(_legacy_settings().get("username") or "").strip()
    if not wanted:
        return str(accounts[0].get("username") or "").strip()
    match = next((a for a in accounts if str(a.get("id") or "") == wanted), None)
    return str((match or {}).get("username") or "").strip()


def resolve_username(account_id: str | None = None, *, data_dir: Path | None = None) -> str:
    """Return cached username for ``account_id``, resolving via ``/user`` if needed."""
    acc = resolve_account(account_id, require_auth=True, data_dir=data_dir)
    assert acc is not None
    if acc["username"]:
        return str(acc["username"])
    if acc["auth_type"] == "github_app_installation":
        raise GithubCoreError(
            f"GitHub App installation '{acc['name']}' has no login recorded. "
            "Sync installations in GitHub Core (/plugins/github_core/).",
            code="missing_username",
            account_name=acc["name"],
        )
    res = test_connection(acc["id"], data_dir=data_dir)
    if res["ok"] and res.get("username"):
        return str(res["username"])
    raise GithubCoreError(
        str(res.get("error") or "Could not resolve GitHub username."),
        code="missing_username",
        account_name=acc["name"],
    )


def headers(
    *,
    accept: str = ACCEPT,
    account_id: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Build GitHub API headers authenticated for ``account_id`` (or default)."""
    h: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
        "Accept-Encoding": "identity",
    }
    resolved_token = token if token is not None else get_token(account_id, data_dir=data_dir)
    if resolved_token:
        h["Authorization"] = f"Bearer {resolved_token}"
    return h


def _can_retry_401(account: dict[str, Any] | None) -> bool:
    if account is None:
        return False
    return account.get("auth_type") in {"github_app_user", "github_app_installation"}


def request_json(
    url: str,
    *,
    timeout: int = HTTP_TIMEOUT_S,
    account_id: str | None = None,
    accept: str = ACCEPT,
    data_dir: Path | None = None,
) -> Any:
    """GET a GitHub JSON endpoint with multi-account auth & automatic 401 refresh.

    Raises ``GithubAcceptedError`` on HTTP 202 (async stats endpoints).
    """
    account = resolve_account(account_id, require_auth=False, data_dir=data_dir)
    token = str((account or {}).get("token") or "")

    for attempt in range(2):
        req = urllib.request.Request(
            url,
            headers=headers(accept=accept, token=token),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 202:
                    raise GithubAcceptedError(f"GitHub still computing stats for {url}")
                body = _decode_body(resp).decode("utf-8")
                if not body.strip():
                    return []
                return json.loads(body)
        except urllib.error.HTTPError as err:
            if err.code == 401 and attempt == 0 and _can_retry_401(account):
                with contextlib.suppress(Exception):
                    err.close()
                refreshed = get_account(
                    account["id"] if account else None, force_refresh=True, data_dir=data_dir
                )
                token = str((refreshed or {}).get("token") or "")
                if token:
                    continue
            raise


def request_graphql(
    query: str,
    variables: dict[str, Any],
    *,
    timeout: int = HTTP_TIMEOUT_S,
    account_id: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """POST a GraphQL query to ``api.github.com/graphql`` with multi-account auth."""
    account = resolve_account(account_id, require_auth=True, data_dir=data_dir)
    assert account is not None
    token = str(account["token"])
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")

    for attempt in range(2):
        h = headers(token=token)
        h["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{GITHUB_API_BASE}/graphql",
            data=body,
            headers=h,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(_decode_body(resp).decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 401 and attempt == 0 and _can_retry_401(account):
                with contextlib.suppress(Exception):
                    err.close()
                refreshed = get_account(account["id"], force_refresh=True, data_dir=data_dir)
                token = str((refreshed or {}).get("token") or "")
                if token:
                    continue
            raise


def list_accessible_repos(
    account_id: str | None = None,
    *,
    user: str = "",
    max_pages: int = 5,
    data_dir: Path | None = None,
) -> list[str]:
    """List non-archived ``owner/repo`` entries accessible to ``account_id`` (or ``user``).

    * ``github_app_installation`` → ``GET /installation/repositories`` (org & personal repos)
    * ``github_app_user`` / ``pat`` → ``GET /user/repos`` (owned, collaborator & org repos)
    * Unauthenticated fallback → ``GET /users/{user}/repos``

    Results are ordered most recently pushed first for every auth type
    (``/installation/repositories`` has no ``sort`` parameter, so it is sorted here).
    """
    account = resolve_account(account_id, require_auth=False, data_dir=data_dir)
    out: list[str] = []
    seen: set[str] = set()
    pushed: dict[str, str] = {}

    for page in range(1, max_pages + 1):
        if account is not None and account.get("auth_type") == "github_app_installation":
            url = f"{GITHUB_API_BASE}/installation/repositories?per_page=100&page={page}"
            payload = request_json(url, account_id=account["id"], data_dir=data_dir)
            batch = (payload or {}).get("repositories") if isinstance(payload, dict) else []
        elif account is not None and account.get("token"):
            qs = urllib.parse.urlencode(
                {
                    "sort": "pushed",
                    "direction": "desc",
                    "per_page": "100",
                    "page": str(page),
                    "affiliation": "owner,collaborator,organization_member",
                }
            )
            url = f"{GITHUB_API_BASE}/user/repos?{qs}"
            batch = request_json(url, account_id=account["id"], data_dir=data_dir)
        else:
            clean_user = (user or "").strip()
            if not clean_user:
                return []
            qs = urllib.parse.urlencode(
                {"type": "owner", "sort": "pushed", "per_page": "100", "page": str(page)}
            )
            url = f"{GITHUB_API_BASE}/users/{urllib.parse.quote(clean_user, safe='')}/repos?{qs}"
            batch = request_json(url, data_dir=data_dir)

        if not isinstance(batch, list):
            break
        for repo in batch:
            if not isinstance(repo, dict) or repo.get("archived"):
                continue
            full = str(repo.get("full_name") or "").strip()
            key = full.lower()
            if full and key not in seen:
                seen.add(key)
                out.append(full)
                pushed[full] = str(repo.get("pushed_at") or "")
        if len(batch) < 100:
            break
    # Stable sort: keeps API order for repos without ``pushed_at``.
    out.sort(key=lambda full: pushed.get(full, ""), reverse=True)
    return out


# ----- Diagnostics & user-facing error formatting --------------------


def _format_rate_limit_reset(headers_obj: Any, *, now: float | None = None) -> str:
    now_ts = time.time() if now is None else now
    reset_raw = _header_get(headers_obj, "X-RateLimit-Reset")
    if reset_raw:
        with contextlib.suppress(ValueError):
            reset_ts = float(reset_raw)
            if reset_ts > 0:
                utc_str = datetime.fromtimestamp(reset_ts, tz=UTC).strftime("%H:%M UTC")
                delta = max(0, int(reset_ts - now_ts))
                if delta >= 60:
                    mins = (delta + 59) // 60
                    return f"Resets at {utc_str} (in {mins}m)."
                if delta > 0:
                    return f"Resets at {utc_str} (in {delta}s)."
                return f"Resets at {utc_str}."
    retry_raw = _header_get(headers_obj, "Retry-After")
    if retry_raw:
        with contextlib.suppress(ValueError):
            retry_s = int(float(retry_raw))
            if retry_s >= 60:
                return f"Retry after {(retry_s + 59) // 60}m."
            if retry_s > 0:
                return f"Retry after {retry_s}s."
    return ""


def coerce_error(
    err: Exception,
    *,
    account_name: str = "",
    repo: str = "",
    user: str = "",
    endpoint: str = "",
) -> str:
    """Turn any GitHub/network/core exception into a clear, actionable message
    for display inside a widget cell."""
    if isinstance(err, GithubCoreError):
        return err.message

    who = f" for '{account_name}'" if account_name else ""
    target = f" '{repo}'" if repo else (f" @{user}" if user else (f" ({endpoint})" if endpoint else ""))

    if isinstance(err, urllib.error.HTTPError):
        detail = ""
        rate_remaining = _header_get(err.headers, "X-RateLimit-Remaining")
        reset_hint = _format_rate_limit_reset(err.headers)
        with contextlib.suppress(Exception):
            raw = err.read(_ERROR_DETAIL_BYTES).decode("utf-8", errors="replace").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        errors = parsed.get("errors")
                        if (
                            isinstance(errors, list)
                            and errors
                            and isinstance(errors[0], dict)
                            and errors[0].get("message")
                        ):
                            detail = str(errors[0]["message"]).strip()
                        elif parsed.get("message"):
                            detail = str(parsed["message"]).strip()
                except ValueError:
                    detail = raw[:180]
        with contextlib.suppress(Exception):
            err.close()

        if err.code == 401:
            return (
                f"GitHub HTTP 401{who}: authentication expired or invalid. "
                "Re-authorize or test the account in GitHub Core (/plugins/github_core/)."
            )
        if err.code == 403:
            if rate_remaining == "0" or "rate limit" in detail.lower():
                extra = f" {reset_hint}" if reset_hint else ""
                if not account_name:
                    return (
                        f"GitHub HTTP 403: unauthenticated rate limit reached.{extra} "
                        "Connect a GitHub App in GitHub Core (/plugins/github_core/) for 5,000 req/hr."
                    )
                return f"GitHub HTTP 403{who}: API rate limit reached.{extra}"
            reason = f" ({detail})" if detail else ""
            return (
                f"GitHub HTTP 403{who}: access denied{target}{reason}. "
                "Check the GitHub App installation and read-only permissions in GitHub Core (/plugins/github_core/)."
            )
        if err.code == 404:
            if repo:
                return (
                    f"GitHub HTTP 404{who}: repository '{repo}' not found or not granted to this account. "
                    "Check the repo name or install the GitHub App on its org."
                )
            if user:
                return f"GitHub HTTP 404{who}: user '@{user}' was not found on GitHub."
            return f"GitHub HTTP 404{who}: resource not found{target}."
        if err.code == 422:
            reason = f": {detail}" if detail else ""
            return f"GitHub HTTP 422{who}: query rejected{reason}."
        if err.code == 429:
            extra = f" {reset_hint}" if reset_hint else " Try again in a few minutes."
            return f"GitHub HTTP 429{who}: rate limit reached.{extra}"
        if 500 <= err.code <= 599:
            return f"GitHub HTTP {err.code}: GitHub is temporarily unavailable. Will retry on next refresh."
        reason = detail or str(err.reason or "request failed")
        return f"GitHub HTTP {err.code}{who}: {reason}"

    if isinstance(err, urllib.error.URLError):
        return f"Cannot reach GitHub ({err.reason}). Check network connectivity."
    if isinstance(err, TimeoutError):
        return f"GitHub timed out after {HTTP_TIMEOUT_S}s. Try refreshing shortly."
    if isinstance(err, json.JSONDecodeError):
        return "GitHub returned invalid JSON. Try refreshing shortly."
    return f"{type(err).__name__}: {err}"


def coerce_graphql_error(payload: Any, *, account_name: str = "", auth_type: str = "") -> str | None:
    """Inspect a GraphQL response dict and return an actionable error string if any."""
    if not isinstance(payload, dict):
        return "GitHub GraphQL returned an unexpected response."
    errors = payload.get("errors")
    if not errors:
        return None
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    msg = str(first.get("message") or "GraphQL query failed").strip()
    err_type = str(first.get("type") or "").strip().upper()
    who = f" for '{account_name}'" if account_name else ""
    if auth_type == "github_app_installation" and (
        err_type in {"FORBIDDEN", "INSUFFICIENT_SCOPES"} or "resource not accessible" in msg.lower()
    ):
        return (
            f"GitHub GraphQL{who}: {msg}. Installation-only accounts cannot access some user/org GraphQL "
            "fields; authorize a user on this GitHub App in GitHub Core (/plugins/github_core/)."
        )
    return f"GitHub GraphQL{who}: {msg}"


# ----- live connection testing ---------------------------------------


def test_connection(account_id: str, *, data_dir: Path | None = None) -> dict[str, Any]:
    """Verify connectivity & permissions for ``account_id`` and persist status."""
    try:
        account = get_account(account_id, data_dir=data_dir)
    except GithubCoreError as err:
        return {"ok": False, "username": None, "detail": None, "error": err.message}

    if account is None:
        return {
            "ok": False,
            "username": None,
            "detail": None,
            "error": f"GitHub Account '{account_id}' not found.",
        }

    auth_type = account["auth_type"]
    username: str | None = account["username"] or None
    detail: str | None = None
    error_msg: str | None = None

    try:
        if auth_type == "github_app_installation":
            token = mint_installation_token(
                account["app_ref"],
                int(account["installation_id"]),
                force_refresh=True,
                data_dir=data_dir,
            )
            req = urllib.request.Request(
                f"{GITHUB_API_BASE}/installation/repositories?per_page=1",
                headers=headers(token=token),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                remaining = _header_get(resp.headers, "X-RateLimit-Remaining")
                payload = json.loads(_decode_body(resp).decode("utf-8"))
            total_repos = int((payload or {}).get("total_count") or 0)
            detail = f"{total_repos} repo{'s' if total_repos != 1 else ''} accessible"
            if remaining:
                detail += f" · {remaining} req remaining"
        else:
            token = account["token"]
            if not token:
                raise GithubCoreError("Token is missing.", code="auth_required")
            req = urllib.request.Request(
                f"{GITHUB_API_BASE}/user",
                headers=headers(token=token),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                remaining = _header_get(resp.headers, "X-RateLimit-Remaining")
                payload = json.loads(_decode_body(resp).decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("login"), str) and payload["login"].strip():
                username = payload["login"].strip()
                detail = f"@{username}" + (f" · {remaining} req remaining" if remaining else "")
            else:
                error_msg = "GitHub /user response did not include a username."
    except Exception as err:
        error_msg = coerce_error(err, account_name=account["name"])

    data = _load_raw(data_dir)
    record = next((a for a in data["accounts"] if str(a.get("id") or "") == account_id), None)
    if record is not None:
        if error_msg is None:
            if username:
                record["username"] = username
            record["last_tested_at"] = _now_iso()
            record["last_error"] = None
        else:
            record["last_error"] = error_msg
        _save_raw(data, data_dir)

    if error_msg is None:
        return {"ok": True, "username": username, "detail": detail, "error": None}
    return {"ok": False, "username": None, "detail": None, "error": error_msg}


def test_all_accounts(*, data_dir: Path | None = None) -> dict[str, Any]:
    """Run ``test_connection`` across every saved GitHub account."""
    accounts = list_accounts(data_dir=data_dir)
    ok_count = 0
    failed: list[tuple[str, str]] = []
    for acc in accounts:
        res = test_connection(acc["id"], data_dir=data_dir)
        if res["ok"]:
            ok_count += 1
        else:
            failed.append((acc["name"], str(res.get("error") or "Connection failed")))
    return {"tested": len(accounts), "ok_count": ok_count, "failed": failed}


def _manifest_action_url(org: str, state: str) -> str:
    if org:
        return (
            f"{GITHUB_WEB_BASE}/organizations/{urllib.parse.quote(org, safe='')}"
            f"/settings/apps/new?state={urllib.parse.quote(state, safe='')}"
        )
    return f"{GITHUB_WEB_BASE}/settings/apps/new?state={urllib.parse.quote(state, safe='')}"


# ----- admin blueprint -----------------------------------------------


def blueprint() -> Blueprint:
    bp = Blueprint("github_core_admin", __name__, template_folder="templates")

    def _render_manifest_step(
        spec: dict[str, str],
        *,
        remaining: list[dict[str, str]],
        step: int,
        total: int,
        created_names: list[str],
        prev_created: str = "",
    ) -> str:
        base_url = _public_base_url()
        manifest = build_app_manifest(spec["name"], base_url)
        state = _create_state(
            "manifest",
            {
                "org": spec["org"],
                "name": spec["name"],
                "remaining": remaining,
                "step": step,
                "total": total,
                "created_names": created_names,
            },
        )
        return render_template(
            "github_core/manifest_post.html",
            action_url=_manifest_action_url(spec["org"], state),
            manifest_json=json.dumps(manifest),
            app_name=spec["name"],
            org=spec["org"],
            step=step,
            total=total,
            prev_created=prev_created,
            cancel_url=url_for("github_core_admin.index"),
        )

    @bp.get("/")
    def index() -> str:
        apps = list_apps()
        accounts = list_accounts()
        pat_accounts = [a for a in accounts if a["auth_type"] == "pat"]
        return render_template(
            "github_core/index.html",
            apps=apps,
            accounts=accounts,
            pat_accounts=pat_accounts,
            legacy=_legacy_settings(),
            base_url=_public_base_url(),
            suggested_app_name="" if apps else "Tesserae GitHub",
        )

    # -- GitHub App Manifest 1-click creation (single or batch queue) --

    @bp.post("/apps/manifest/start")
    def manifest_start_route() -> str | Response:
        raw_names = request.form.getlist("name")
        raw_orgs = request.form.getlist("org")
        count = max(len(raw_names), len(raw_orgs), 1)
        specs: list[dict[str, str]] = []

        for idx in range(count):
            name = (raw_names[idx] if idx < len(raw_names) else "").strip()
            org = (raw_orgs[idx] if idx < len(raw_orgs) else "").strip().lstrip("@")
            if org and not _OWNER_RE.match(org):
                flash(f"Invalid GitHub organization name '{org}'.", "warn")
                return redirect(url_for("github_core_admin.index"))
            if not name and not org:
                continue
            if not name:
                name = f"Tesserae {org}" if org else "Tesserae GitHub"
            specs.append({"name": name, "org": org})

        if not specs:
            specs.append({"name": "Tesserae GitHub", "org": ""})

        return _render_manifest_step(
            specs[0],
            remaining=specs[1:],
            step=1,
            total=len(specs),
            created_names=[],
        )

    @bp.get("/apps/manifest/callback")
    def manifest_callback_route() -> str | Response:
        code = (request.args.get("code") or "").strip()
        state = (request.args.get("state") or "").strip()
        pending = _consume_state(state, expected_kind="manifest")
        if pending is None:
            flash("GitHub App creation state expired or invalid. Try creating the app again.", "warn")
            return redirect(url_for("github_core_admin.index"))

        created_names = [str(n) for n in (pending.get("created_names") or []) if str(n)]
        remaining = [
            {"name": str(item.get("name") or "Tesserae GitHub"), "org": str(item.get("org") or "")}
            for item in (pending.get("remaining") or [])
            if isinstance(item, dict)
        ]
        step = int(pending.get("step") or 1)
        total = int(pending.get("total") or (step + len(remaining)))

        try:
            created = convert_manifest_code(code)
        except GithubCoreError as err:
            if created_names:
                flash(
                    f"Created {len(created_names)} GitHub App(s) ({', '.join(created_names)}), "
                    f"then failed on step {step}: {err.message}",
                    "warn",
                )
            else:
                flash(f"Could not finish creating GitHub App: {err.message}", "warn")
            return redirect(url_for("github_core_admin.index"))

        created_names.append(created["name"])
        if remaining:
            return _render_manifest_step(
                remaining[0],
                remaining=remaining[1:],
                step=step + 1,
                total=total,
                created_names=created_names,
                prev_created=created["name"],
            )

        if len(created_names) > 1:
            flash(
                f"Created {len(created_names)} GitHub Apps: {', '.join(created_names)}. "
                "Next, click 'Install on GitHub' on each app to attach your personal account or organizations.",
                "ok",
            )
        else:
            flash(
                f"Created GitHub App '{created['name']}' (App ID {created['app_id']}). "
                "Next, click 'Install on GitHub' to choose your personal account or organizations.",
                "ok",
            )
        return redirect(url_for("github_core_admin.index"))

    # -- Manual GitHub App registration (single or batch) & editing --

    @bp.post("/apps/manual")
    def create_app_manual_route() -> Response:
        names = request.form.getlist("name")
        app_ids = request.form.getlist("app_id")
        client_ids = request.form.getlist("client_id")
        client_secrets = request.form.getlist("client_secret")
        pems = request.form.getlist("pem")
        count = max(len(names), len(app_ids), len(client_ids), len(client_secrets), len(pems))
        entries = [
            {
                "name": names[i] if i < len(names) else "",
                "app_id": app_ids[i] if i < len(app_ids) else "",
                "client_id": client_ids[i] if i < len(client_ids) else "",
                "client_secret": client_secrets[i] if i < len(client_secrets) else "",
                "pem": pems[i] if i < len(pems) else "",
            }
            for i in range(count)
        ]
        result = create_apps_manual_batch(entries)
        created = result["created"]
        errors = result["errors"]

        if len(created) == 1:
            app = created[0]
            flash(f"Saved GitHub App '{app['name']}' (App ID {app['app_id']}).", "ok")
        elif len(created) > 1:
            summary = ", ".join(f"{a['name']} (#{a['app_id']})" for a in created)
            flash(f"Saved {len(created)} GitHub Apps: {summary}.", "ok")
        if errors:
            flash(" · ".join(errors), "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/apps/<app_ref>/edit")
    def edit_app_route(app_ref: str) -> Response:
        name = (request.form.get("name") or "").strip()
        app_id = (request.form.get("app_id") or "").strip()
        client_id = request.form.get("client_id")
        client_secret = (request.form.get("client_secret") or "").strip()
        pem = (request.form.get("pem") or "").strip()
        try:
            updated = update_app(
                app_ref,
                name=name,
                app_id=app_id or None,
                client_id=client_id if client_id is not None else None,
                client_secret=client_secret or None,
                pem=pem or None,
            )
        except (ValueError, GithubCoreError) as err:
            flash(str(err), "warn")
            return redirect(url_for("github_core_admin.index"))
        if updated is None:
            abort(404)
        flash(f"Updated GitHub App '{updated['name']}' (App ID {updated['app_id']}).", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/apps/<app_ref>/test")
    def test_app_route(app_ref: str) -> Response:
        apps = list_apps()
        match = next((a for a in apps if a["id"] == app_ref), None)
        if match is None:
            abort(404)
        result = test_app(app_ref)
        if result["ok"]:
            flash(f"GitHub App '{match['name']}' verified ({result['detail']}).", "ok")
        else:
            flash(f"GitHub App test failed for '{match['name']}': {result['error']}", "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/apps/sync-all")
    def sync_all_apps_route() -> Response:
        result = sync_all_apps()
        if result["synced_apps"]:
            flash(
                f"Synced {result['total_installations']} installation(s) across "
                f"{result['synced_apps']} GitHub App(s).",
                "ok",
            )
        elif not result["errors"]:
            flash("No GitHub Apps registered yet.", "warn")
        if result["errors"]:
            flash(" · ".join(result["errors"]), "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/apps/<app_ref>/sync")
    def sync_app_route(app_ref: str) -> Response:
        try:
            synced = sync_app_installations(app_ref)
        except GithubCoreError as err:
            flash(f"Installation sync failed: {err.message}", "warn")
            return redirect(url_for("github_core_admin.index"))
        if not synced:
            flash(
                "No installations found for this GitHub App yet. "
                "Click 'Install on GitHub' to install it on your account or organizations.",
                "warn",
            )
        else:
            names = ", ".join(a["name"] for a in synced[:5])
            suffix = f" (+{len(synced) - 5} more)" if len(synced) > 5 else ""
            flash(f"Synced {len(synced)} installation(s): {names}{suffix}.", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.get("/apps/installed")
    def app_installed_route() -> Response:
        """Post-install ``setup_url`` target from GitHub; syncs installations automatically."""
        raw_inst = (request.args.get("installation_id") or "").strip()
        inst_id = int(raw_inst) if raw_inst.isdigit() else None
        app_ref = _find_app_for_installation(inst_id) if inst_id is not None else None
        if app_ref:
            try:
                synced = sync_app_installations(app_ref)
                flash(f"GitHub App installed and synced ({len(synced)} installation(s)).", "ok")
            except GithubCoreError as err:
                flash(f"GitHub App installed, but sync failed: {err.message}", "warn")
        else:
            flash("GitHub App installation updated. Click 'Sync installations' on your app below.", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.get("/apps/<app_ref>/oauth/start")
    def oauth_start_route(app_ref: str) -> Response:
        redirect_uri = f"{_public_base_url()}/plugins/github_core/oauth/callback"
        state = _create_state("oauth", {"app_ref": app_ref, "redirect_uri": redirect_uri})
        try:
            url = build_oauth_authorize_url(app_ref, redirect_uri, state)
        except GithubCoreError as err:
            flash(err.message, "warn")
            return redirect(url_for("github_core_admin.index"))
        return redirect(url)

    @bp.get("/oauth/callback")
    def oauth_callback_route() -> Response:
        if request.args.get("error"):
            desc = request.args.get("error_description") or request.args.get("error")
            flash(f"GitHub authorization cancelled or failed: {desc}", "warn")
            return redirect(url_for("github_core_admin.index"))

        code = (request.args.get("code") or "").strip()
        state = (request.args.get("state") or "").strip()
        raw_inst = (request.args.get("installation_id") or "").strip()
        inst_id = int(raw_inst) if raw_inst.isdigit() else None

        app_ref: str | None = None
        redirect_uri = f"{_public_base_url()}/plugins/github_core/oauth/callback"

        if state:
            pending = _consume_state(state, expected_kind="oauth")
            if pending is None:
                flash("GitHub OAuth state expired or invalid. Try authorizing again.", "warn")
                return redirect(url_for("github_core_admin.index"))
            app_ref = str(pending.get("app_ref") or "")
            redirect_uri = str(pending.get("redirect_uri") or redirect_uri)
        elif inst_id is not None:
            # request_oauth_on_install sends code + installation_id without a state param.
            app_ref = _find_app_for_installation(inst_id)

        if not code or not app_ref:
            flash("Could not match the GitHub OAuth callback to a registered GitHub App.", "warn")
            return redirect(url_for("github_core_admin.index"))

        try:
            acc = exchange_oauth_code(app_ref, code, redirect_uri, installation_id=inst_id)
        except GithubCoreError as err:
            flash(f"Could not complete GitHub authorization: {err.message}", "warn")
            return redirect(url_for("github_core_admin.index"))

        if inst_id is not None:
            with contextlib.suppress(GithubCoreError):
                sync_app_installations(app_ref)

        flash(f"Authorized GitHub user account '{acc['name']}' (@{acc['username']}).", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/apps/<app_ref>/delete")
    def delete_app_route(app_ref: str) -> Response:
        if not delete_app(app_ref):
            abort(404)
        flash("Deleted GitHub App and its linked accounts.", "ok")
        return redirect(url_for("github_core_admin.index"))

    # -- Personal Access Tokens (single or batch) & Account management --

    @bp.post("/accounts/pat")
    def create_pat_account_route() -> Response:
        names = request.form.getlist("name")
        pats = request.form.getlist("pat")
        usernames = request.form.getlist("username")
        verify = (request.form.get("verify_on_save") or "").strip().lower() in {"1", "true", "on", "yes"}
        count = max(len(names), len(pats), len(usernames))
        entries = [
            {
                "name": names[i] if i < len(names) else "",
                "pat": pats[i] if i < len(pats) else "",
                "username": usernames[i] if i < len(usernames) else "",
            }
            for i in range(count)
        ]
        result = create_accounts_batch(entries, verify=verify)
        created = result["created"]
        errors = result["errors"]

        if len(created) == 1:
            acc = created[0]
            if verify and result["verified"] == 1 and acc.get("username"):
                flash(f"Added and verified PAT account '{acc['name']}' (@{acc['username']}).", "ok")
            else:
                flash(f"Added PAT account '{acc['name']}'. Click 'Test' to verify and resolve its username.", "ok")
        elif len(created) > 1:
            names_str = ", ".join(a["name"] for a in created)
            if verify:
                flash(
                    f"Added {len(created)} PAT accounts ({names_str}); verified {result['verified']}/{len(created)}.",
                    "ok",
                )
            else:
                flash(
                    f"Added {len(created)} PAT accounts: {names_str}. Click 'Test all' to verify them.",
                    "ok",
                )
        if errors:
            flash(" · ".join(errors), "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/accounts/test-all")
    def test_all_accounts_route() -> Response:
        result = test_all_accounts()
        if not result["tested"]:
            flash("No GitHub Accounts connected yet.", "warn")
            return redirect(url_for("github_core_admin.index"))
        if not result["failed"]:
            flash(f"All {result['ok_count']} GitHub Account(s) passed live connection tests.", "ok")
        else:
            if result["ok_count"]:
                flash(
                    f"Verified {result['ok_count']} of {result['tested']} GitHub Account(s).",
                    "ok",
                )
            failed_msg = " · ".join(f"{name}: {err}" for name, err in result["failed"])
            flash(f"Failed: {failed_msg}", "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/accounts/<account_id>/default")
    def set_default_account_route(account_id: str) -> Response:
        updated = set_default_account(account_id)
        if updated is None:
            abort(404)
        flash(f"Set '{updated['name']}' as the default GitHub Account.", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/accounts/<account_id>/edit")
    def edit_account_route(account_id: str) -> Response:
        name = (request.form.get("name") or "").strip()
        pat = (request.form.get("pat") or "").strip()
        username = request.form.get("username")
        if not name:
            flash("Account name is required.", "warn")
            return redirect(url_for("github_core_admin.index"))
        updated = update_account(
            account_id,
            name=name,
            pat=pat or None,
            username=username if username is not None else None,
        )
        if updated is None:
            abort(404)
        flash(f"Updated GitHub Account '{updated['name']}'.", "ok")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/accounts/<account_id>/test")
    def test_account_route(account_id: str) -> Response:
        accounts = list_accounts()
        match = next((a for a in accounts if a["id"] == account_id), None)
        if match is None:
            abort(404)
        result = test_connection(account_id)
        if result["ok"]:
            detail = result.get("detail") or (f"@{result['username']}" if result.get("username") else "OK")
            flash(f"Connected '{match['name']}' to GitHub ({detail}).", "ok")
        else:
            flash(f"Connection test failed for '{match['name']}': {result['error']}", "warn")
        return redirect(url_for("github_core_admin.index"))

    @bp.post("/accounts/<account_id>/delete")
    def delete_account_route(account_id: str) -> Response:
        accounts = list_accounts()
        match = next((a for a in accounts if a["id"] == account_id), None)
        if match is None:
            abort(404)
        delete_account(account_id)
        flash(f"Deleted GitHub Account '{match['name']}'.", "ok")
        return redirect(url_for("github_core_admin.index"))

    return bp
