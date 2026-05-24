"""TechShareSession — HTTP session with cookie persistence + CSRF management.

Pattern: a thin wrapper around requests.Session that adds:
  - automatic CSRF-token fetch + injection on POSTs to /api/*
  - cookie jar persistence to ~/Library/Application Support/voxhora-techshare-agent/<user>/cookies.pickle
  - login flow (form POST to /Account/LogOn with ASP.NET MVC anti-forgery token)
  - retry on 403 "CSRF token validation failed" → refresh + replay once

The HttpOnly session cookie is owned by the server; we just persist whatever
cookies requests.Session collects after a successful login.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import keyring
import requests

from . import config

log = logging.getLogger(__name__)


class TechShareAuthError(RuntimeError):
    """Login or session-revalidation failed."""


class TechShareSession:
    """Authenticated HTTP session against attorney.techsharetx.gov."""

    def __init__(self, username: str | None = None) -> None:
        self.username = username or self._default_username()
        self._session = requests.Session()
        self._csrf_token: str | None = None
        self._load_cookies()

    # ----- cookie persistence -----

    def _load_cookies(self) -> None:
        path = config.cookies_path(self.username)
        if not path.exists():
            log.debug("no persisted cookies for %s", self.username)
            return
        try:
            with open(path, "rb") as f:
                self._session.cookies.update(pickle.load(f))
            log.info("loaded %d cookies for %s", len(self._session.cookies), self.username)
        except Exception as e:
            log.warning("failed to load cookies (%s); ignoring", e)

    def _save_cookies(self) -> None:
        path = config.cookies_path(self.username)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self._session.cookies, f)
        log.debug("saved %d cookies", len(self._session.cookies))

    # ----- credentials -----

    @staticmethod
    def _default_username() -> str:
        import getpass
        return getpass.getuser()

    def _stored_password(self) -> str | None:
        return keyring.get_password(config.KEYCHAIN_SERVICE, self.username)

    def store_credentials(self, techshare_username: str, techshare_password: str) -> None:
        """Save TechShare credentials in macOS Keychain.

        The keychain entry stores the TechShare username AS the macOS user's
        password value (a JSON blob with both fields). For v2 multi-tenant
        we'll likely store them under separate keys per subscriber.
        """
        import json
        keyring.set_password(
            config.KEYCHAIN_SERVICE,
            self.username,
            json.dumps({"username": techshare_username, "password": techshare_password}),
        )

    def _retrieve_credentials(self) -> tuple[str, str]:
        import json
        raw = self._stored_password()
        if not raw:
            raise TechShareAuthError(
                f"No TechShare credentials in macOS Keychain for user '{self.username}'. "
                f"Run: voxhora-techshare-agent login"
            )
        creds = json.loads(raw)
        return creds["username"], creds["password"]

    # ----- login flow -----

    def login(self) -> None:
        """Programmatic login against TechShare's Ember.js login endpoint.

        Discovered from /App/controllers/login.js + /App/routes/login.js
        (2026-05-25 recon, source-reading):

          POST /api/auth
          Content-Type: application/x-www-form-urlencoded
          Body: username=<un>&password=<pw>&errorMessage=&isResource=

        On success the server sets an HttpOnly session cookie and returns
        JSON with three remediation flags:

          { pwIsExpired: bool, needsConfirmation: bool, requiresMfa: bool }

        If any flag is true the user would normally be routed to a
        remediation flow in the browser; here we raise a TechShareAuthError
        with a clear message so the operator can resolve manually before
        retrying.
        """
        techshare_user, techshare_pass = self._retrieve_credentials()

        auth_url = f"{config.TECHSHARE_BASE_URL}/api/auth"
        form = {
            "username": techshare_user,
            "password": techshare_pass,
            "errorMessage": "",
            "isResource": "",
        }
        # jQuery $.post default — form-encoded, NOT JSON
        resp = self._session.post(
            auth_url,
            data=form,
            headers={"Accept": "application/json"},
            allow_redirects=False,
            timeout=30,
        )

        if resp.status_code == 400:
            # The login controller highlights validation errors on 400
            raise TechShareAuthError(
                f"Login rejected by server (400): {resp.text[:300]}. "
                f"Likely bad username or password."
            )
        if resp.status_code >= 400:
            raise TechShareAuthError(
                f"POST /api/auth returned {resp.status_code}: {resp.text[:300]}"
            )

        # Successful POST returns JSON with remediation flags
        try:
            payload = resp.json()
        except Exception as e:
            raise TechShareAuthError(
                f"Login response was not JSON ({e}); body: {resp.text[:300]}"
            )

        if payload.get("requiresMfa"):
            raise TechShareAuthError(
                "Account requires MFA (multi-factor auth). Agent v1 does "
                "not handle MFA flows yet. Resolve via the browser, then "
                "retry agent login."
            )
        if payload.get("pwIsExpired"):
            raise TechShareAuthError(
                "TechShare password is expired. Reset it via the browser, "
                "update via `voxhora-techshare-agent login`."
            )
        if payload.get("needsConfirmation"):
            raise TechShareAuthError(
                "Account needs confirmation (likely first-login or contact-"
                "info verification). Complete via the browser, then retry."
            )

        # Refresh CSRF token after login (mirrors the controller's success handler)
        if not self._fetch_csrf_token():
            raise TechShareAuthError(
                "Login HTTP succeeded but /api/csrf-token failed afterwards. "
                "Session may not be valid."
            )

        self._save_cookies()
        log.info("login successful for user '%s'", techshare_user)

    # ----- CSRF -----

    def _fetch_csrf_token(self) -> str | None:
        url = f"{config.TECHSHARE_BASE_URL}/api/csrf-token"
        try:
            r = self._session.get(url, timeout=15)
            if r.status_code != 200:
                log.warning("/api/csrf-token returned %d", r.status_code)
                return None
            payload = r.json()
            token = payload.get("token")
            self._csrf_token = token
            return token
        except Exception as e:
            log.warning("failed to fetch CSRF token: %s", e)
            return None

    def csrf_token(self, force_refresh: bool = False) -> str:
        if force_refresh or not self._csrf_token:
            tok = self._fetch_csrf_token()
            if not tok:
                raise TechShareAuthError(
                    "Could not retrieve CSRF token; session may be expired."
                )
        return self._csrf_token  # type: ignore[return-value]

    # ----- generic API helpers -----

    def api_post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to attorney.techsharetx.gov/api/<path> with CSRF header.

        Auto-retries once on 403 "CSRF token validation failed" by refreshing
        the token. Returns parsed JSON.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = f"{config.TECHSHARE_BASE_URL}{path}"

        for attempt in (1, 2):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRF-Token": self.csrf_token(),
            }
            r = self._session.post(url, json=body, headers=headers, timeout=60)
            if r.status_code == 403 and "csrf" in r.text.lower() and attempt == 1:
                log.info("CSRF token rejected; refreshing and retrying once")
                self.csrf_token(force_refresh=True)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    def proxy_get(self, service_id: str, backend_path: str) -> dict[str, Any]:
        """POST /api/proxy → server proxies a GET to the backend.

        Returns the parsed Collection+JSON response.
        """
        body = {
            "externalServiceId": service_id,
            "Method": "GET",
            "Path": backend_path,
        }
        return self.api_post_json("/api/proxy", body)

    def proxy_download(self, service_id: str, backend_path: str) -> bytes:
        """POST /api/proxy with a download-style endpoint (e.g. /dmefile).

        Returns the raw binary body. Caller writes to disk.
        """
        url = f"{config.TECHSHARE_BASE_URL}/api/proxy"
        body = {
            "externalServiceId": service_id,
            "Method": "GET",
            "Path": backend_path,
        }
        headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrf_token(),
        }
        r = self._session.post(url, json=body, headers=headers, timeout=600, stream=True)
        r.raise_for_status()
        return r.content

    # ----- session-state helpers -----

    def is_authenticated(self) -> bool:
        """Lightweight check: does /api/csrf-token return a token?"""
        return self._fetch_csrf_token() is not None

    def ensure_authenticated(self) -> None:
        """If not authenticated, attempt programmatic login."""
        if self.is_authenticated():
            return
        log.info("session not authenticated; attempting login")
        self.login()
