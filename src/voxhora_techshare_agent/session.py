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
import os
import pickle
from pathlib import Path
from typing import Any

import keyring
import requests

from . import config, storage

log = logging.getLogger(__name__)


class TechShareAuthError(RuntimeError):
    """Login or session-revalidation failed."""


class TechShareSession:
    """Authenticated HTTP session against attorney.techsharetx.gov.

    Two operating modes (2026-05-27 BETA Cloud Agent ship):

      1. **Mac CLI mode** (default) — `TechShareSession()` or
         `TechShareSession(username=...)`. Credentials read from macOS
         Keychain via `keyring`; cookies persisted to
         `~/Library/Application Support/voxhora-techshare-agent/<user>/cookies.pickle`.
         Unchanged behavior — this is what the Voxhora-Mac integration
         and the standalone CLI both use.

      2. **Cloud agent mode** — `TechShareSession(credentials_override=(username, password),
         cookie_storage="memory")`. Bypasses keyring + disk; everything
         lives in process memory. Used by voxhora-agent-cloud (Fly.io
         FastAPI server) where creds come from Fly secrets and cookies
         don't need to outlive the request worker.

    Both modes share the same login + CSRF + proxy_* code paths.
    """

    def __init__(
        self,
        username: str | None = None,
        *,
        credentials_override: tuple[str, str] | None = None,
        cookie_storage: str = "disk",
    ) -> None:
        self.username = username or self._default_username()
        self._session = requests.Session()
        self._csrf_token: str | None = None
        self._credentials_override = credentials_override
        if cookie_storage not in ("disk", "memory"):
            raise ValueError(f"cookie_storage must be 'disk' or 'memory', got {cookie_storage!r}")
        self._cookie_storage = cookie_storage
        if self._cookie_storage == "disk":
            self._load_cookies()

    # ----- cookie persistence -----

    def _load_cookies(self) -> None:
        if self._cookie_storage != "disk":
            return
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
        if self._cookie_storage != "disk":
            return
        path = config.cookies_path(self.username)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-06-01 (audit H4): the persisted jar holds the LIVE
        # DefensePortalAuth/government-portal session. Plain open() relied on the
        # process umask and landed it world-readable (0644), so any local process
        # could read + replay the session. Write owner-only (0600) + atomically
        # via the same helper the rest of storage uses (NamedTemporaryFile is
        # created 0600, the rename preserves it), then chmod the dir + any
        # pre-existing 0644 jar to be safe on already-installed agents.
        storage.atomic_write_bytes(path, pickle.dumps(self._session.cookies))
        try:
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
        except OSError:
            pass
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
        if self._credentials_override is not None:
            return self._credentials_override
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
        # PII discipline (audit M1): don't log the portal username (captured into
        # the shared ~/Voxhora_Logs trace).
        log.info("login successful")

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
        """POST /api/proxy and return the full response body as bytes.

        DEPRECATED for files larger than a few MB — use proxy_download_to_path
        instead. For multi-GB bodycam videos this OOMs by holding the entire
        response in RAM. Kept for small responses (case-list, etc.).
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
        # connect timeout 15s, read timeout None (no cap for slow responses)
        r = self._session.post(url, json=body, headers=headers, timeout=(15, None), stream=True)
        r.raise_for_status()
        return r.content

    def proxy_download_to_path(
        self,
        service_id: str,
        backend_path: str,
        target_path: Path,
        chunk_size: int = 8 * 1024 * 1024,  # 8 MB chunks
    ) -> int:
        """Stream a download from /api/proxy directly to `target_path`.

        Writes to <target>.partial first, atomically renames on success.
        Cleans up the partial file on any exception (including
        KeyboardInterrupt / process termination signal).

        Returns the number of bytes written. Use this for large DME items
        (videos, multi-GB ZIPs) — never holds the full body in RAM.
        """
        import os
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
        # No read-timeout cap — multi-hour video downloads must not die mid-stream.
        # connect=15s, read=None.
        r = self._session.post(url, json=body, headers=headers, timeout=(15, None), stream=True)
        r.raise_for_status()

        target_path.parent.mkdir(parents=True, exist_ok=True)
        partial = target_path.with_suffix(target_path.suffix + ".partial")
        bytes_written = 0
        try:
            with open(partial, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
            os.replace(partial, target_path)
            return bytes_written
        except BaseException:
            # Catch BaseException (covers KeyboardInterrupt / SystemExit
            # too) so .partial files don't pile up on cancellation.
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            raise

    def prep_dme_download(self, service_id: str, dme_url: str) -> tuple[str, str]:
        """Stage a LARGE DME file for download via the web player's prep flow.

        POST /api/dme/download/prep with form fields ExternalServiceId + Url
        (the /dmefile enclosure href). Returns 201 with an empty body. Each
        prep returns a per-file `downloadLink` header (`/download?token=...`).
        The accompanying `DefensePortalAuth` cookie is SESSION-LEVEL: TechShare
        sets it via Set-Cookie on the FIRST prep of a session and does NOT
        re-send it on later preps — the web player keeps it in its cookie jar
        and reuses it for every subsequent video's downloadLink. So we must
        capture it from the first prep that provides it, cache it for the rest
        of the session, and fall back to the jar. (Reading only the per-RESPONSE
        cookie made files 2..N fail with auth=False — the 2026-05-29 bug.)

        This exists because /api/proxy CANNOT serve multi-GB videos — it 500s
        instantly in download mode and hangs forever in stream mode (verified
        2026-05-29). The web player uses this prep flow for every video.

        Returns (download_link, defense_portal_auth_cookie).
        """
        url = f"{config.TECHSHARE_BASE_URL}/api/dme/download/prep"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": self.csrf_token(),
        }
        r = self._session.post(
            url,
            data={"ExternalServiceId": service_id, "Url": dme_url},
            headers=headers,
            timeout=(15, 180),
        )
        r.raise_for_status()
        link = r.headers.get("downloadLink")
        auth = self._resolve_defense_portal_auth(r)
        if not link or not auth:
            raise RuntimeError(
                f"prep returned no downloadLink/DefensePortalAuth "
                f"(status {r.status_code}, link={bool(link)}, auth={bool(auth)})"
            )
        return link, auth

    def _resolve_defense_portal_auth(self, response) -> str | None:
        """Find the session-level DefensePortalAuth cookie set by prep.

        Order: (1) this response's Set-Cookie, (2) the value cached from an
        earlier prep this session, (3) the persisted cookie jar. Iterates
        rather than calling .get() because the jar can legitimately hold more
        than one DefensePortalAuth (one per domain/path) which makes .get()
        raise CookieConflictError. Caches whatever it resolves so subsequent
        preps in the same fetch run reuse it.
        """
        # 1) cookie set on THIS prep response (present on the first prep)
        for ck in response.cookies:
            if ck.name == "DefensePortalAuth" and ck.value:
                self._dpa_cache = ck.value
                return ck.value
        # 2) cached from an earlier prep this session
        cached = getattr(self, "_dpa_cache", None)
        if cached:
            return cached
        # 3) jar fallback (conflict-safe) — take the most recently added
        vals = [ck.value for ck in self._session.cookies if ck.name == "DefensePortalAuth" and ck.value]
        if vals:
            self._dpa_cache = vals[-1]
            return vals[-1]
        return None

    def prepared_download_to_path(
        self,
        download_link: str,
        auth_cookie: str,
        target_path: Path,
        chunk_size: int = 8 * 1024 * 1024,  # 8 MB chunks
    ) -> int:
        """Stream a prepped DME file from the DME content host to disk.

        `download_link` + `auth_cookie` come from prep_dme_download(). The
        DefensePortalAuth cookie is sent ONLY to the DME host (it is a
        different domain than the API host, so the session jar won't attach
        it automatically). Writes <target>.partial then atomically renames;
        cleans up the partial on any exception (mirrors proxy_download_to_path).
        Returns bytes written.
        """
        import os
        url = f"{config.DME_DOWNLOAD_BASE_URL}{download_link}"
        # Read timeout is the gap BETWEEN received chunks, not the total — a
        # genuinely slow-but-progressing multi-GB stream keeps resetting it, so
        # 120s never trips on real transfers. But a STALLED connection (server
        # accepted then sends nothing) would otherwise hang forever with no cap;
        # 120s makes it fail so the caller's retry re-preps + tries again
        # instead of wedging the whole run (2026-05-29).
        r = self._session.get(
            url,
            cookies={"DefensePortalAuth": auth_cookie},
            timeout=(15, 120),
            stream=True,
        )
        r.raise_for_status()

        target_path.parent.mkdir(parents=True, exist_ok=True)
        partial = target_path.with_suffix(target_path.suffix + ".partial")
        bytes_written = 0
        try:
            with open(partial, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
            os.replace(partial, target_path)
            return bytes_written
        except BaseException:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            raise

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
