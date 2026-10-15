"""A password refusal on a mid-run re-login ends the run's sign-ins (2026-10-15).

Before: every download retry re-logged in, and a re-login TechShare turned
down was only a warning — so a changed password cost up to two refused
sign-ins per failing file (TechShare locks the account after about five,
08-10). Now the first refusal in TechShare's own words ends re-logins for the
run; the run still finishes and prints its summary, and main() reports the
refusal so the Mac app turns its password warning on. Anything else — a
collision 400, 5xx, a timeout — keeps today's re-login on every retry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from voxhora_techshare_agent import cli
from voxhora_techshare_agent import session as session_module
from voxhora_techshare_agent.models import DMEItem
from voxhora_techshare_agent.proxy_client import TechShareClient
from voxhora_techshare_agent.session import TechShareAuthError, TechShareSession

# TechShare's real answers (agent logs 08-10 and 10-14; no client data).
MISMATCH = ('Login rejected by server (400): {"message":"The request is invalid.","modelState":'
            '{"password":["Password does not match."]}}. Likely bad username or password.')
LOCKOUT = ('Login rejected by server (400): {"message":"The request is invalid.","modelState":'
           '{"model":["The max number failed login attempts reached. Please wait for 10 minutes to try again."]}}')
EXPIRED = "TechShare password is expired. Reset it via the browser, update via `voxhora-techshare-agent login`."
COLLISION_USER = ('Login rejected by server (400): {"message":"The request is invalid.","modelState":'
                  '{"":["User not found."]}}')
COLLISION_NO_PW = ('Login rejected by server (400): {"message":"The request is invalid.","modelState":'
                   '{"password":["A Password does not exist for this user. Please reset your password '
                   'or contact an administrator."]}}')


@pytest.fixture(autouse=True)
def _clean_run():
    session_module.reset_relogin_refusal()
    yield
    session_module.reset_relogin_refusal()


# ------------------------------------------------------------- the rule

def test_only_techshares_own_password_answers_count():
    for text in (MISMATCH, LOCKOUT, EXPIRED, MISMATCH.upper(), "Login rejected by server (400): Password Is Expired"):
        assert session_module.is_password_refusal(text), text
    for text in (COLLISION_USER, COLLISION_NO_PW,
                 "POST /api/auth returned 503: Service Unavailable",
                 "Login response was not JSON (Expecting value); body: <html>",
                 "Login HTTP succeeded but /api/csrf-token failed afterwards. Session may not be valid.",
                 "Account requires MFA (multi-factor auth).",
                 "Account needs confirmation (likely first-login or contact-info verification).",
                 "No TechShare credentials in macOS Keychain",
                 "Read timed out. (read timeout=60)",
                 # A file name with the words in it, before a collision 400.
                 "Password does not match.pdf — " + COLLISION_USER,
                 # The expired words, but not the agent's own message.
                 "attorney note: techshare password is expired soon"):
        assert not session_module.is_password_refusal(text), text


# ------------------------------------------------------- reauthenticate

def _session(login_error: Exception | None, password: str = "old-test-pw"):
    s = TechShareSession.__new__(TechShareSession)
    s._session = requests.Session()
    s._csrf_token = "TOKEN"
    s._dpa_cache = "DPA"
    s._credentials_override = ("test-user", password)
    calls = {"login": 0}

    def fake_login():
        calls["login"] += 1
        if callable(login_error):
            login_error()
        elif login_error is not None:
            raise login_error

    s.login = fake_login
    return s, calls


def test_a_password_refusal_ends_re_logins_for_the_run():
    s, calls = _session(TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError, match="Password does not match"):
        s.reauthenticate()
    assert session_module.relogin_refusal() == MISMATCH
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    assert calls["login"] == 1, "one refused sign-in, never another in this run"
    # The session is left as it was (2026-07-28 rule still holds).
    assert s._csrf_token == "TOKEN" and s._dpa_cache == "DPA"


@pytest.mark.parametrize("text", [LOCKOUT, EXPIRED])
def test_lockout_and_expired_end_re_logins_too(text):
    s, calls = _session(TechShareAuthError(text))
    with pytest.raises(TechShareAuthError):
        s.reauthenticate()
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    assert calls["login"] == 1


@pytest.mark.parametrize("text", [COLLISION_USER, COLLISION_NO_PW, "POST /api/auth returned 503: x"])
def test_anything_else_keeps_re_logging_in(text):
    s, calls = _session(TechShareAuthError(text))
    for _ in range(3):
        with pytest.raises(TechShareAuthError, match=text[:20].replace("(", r"\(").replace(")", r"\)")):
            s.reauthenticate()
    assert calls["login"] == 3
    assert session_module.relogin_refusal() is None


def test_a_new_saved_password_signs_in_again():
    # The attorney types the new password (Connect & Verify) during a long run.
    s, calls = _session(TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError, match="Password does not match"):
        s.reauthenticate()
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    s._credentials_override = ("test-user", "new-test-pw")
    s.login = lambda: calls.__setitem__("login", calls["login"] + 1)  # TechShare takes it
    s.reauthenticate()
    assert calls["login"] == 2
    assert session_module.relogin_refusal() is None, "nothing to report: the run healed"


def test_a_new_password_that_is_also_refused_holds_again():
    s, calls = _session(TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError):
        s.reauthenticate()
    s._credentials_override = ("test-user", "new-test-pw")
    with pytest.raises(TechShareAuthError, match="Password does not match"):
        s.reauthenticate()
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    assert calls["login"] == 2


def test_a_lockout_is_waited_out_for_15_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(session_module, "_monotonic", lambda: clock[0])
    s, calls = _session(TechShareAuthError(LOCKOUT))
    with pytest.raises(TechShareAuthError, match="max number"):
        s.reauthenticate()
    clock[0] += 15 * 60 - 1
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    assert calls["login"] == 1
    clock[0] += 1
    s.login = lambda: calls.__setitem__("login", calls["login"] + 1)
    s.reauthenticate()
    assert calls["login"] == 2


def test_a_wrong_password_is_never_waited_out(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(session_module, "_monotonic", lambda: clock[0])
    s, calls = _session(TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError):
        s.reauthenticate()
    clock[0] += 6 * 3600
    with pytest.raises(TechShareAuthError, match="re-login skipped"):
        s.reauthenticate()
    assert calls["login"] == 1


def test_the_refused_password_is_not_kept():
    s, _ = _session(TechShareAuthError(MISMATCH), password="old-test-pw")
    with pytest.raises(TechShareAuthError):
        s.reauthenticate()
    held = [v for k, v in vars(session_module).items() if k.startswith("_relogin")]
    assert not any("old-test-pw" in repr(v) for v in held)


def test_a_many_line_answer_is_reported_on_one_line():
    body = 'Login rejected by server (400): {\n  "password": [\n    "Password does not match."\n  ]\n}'
    s, _ = _session(TechShareAuthError(body))
    with pytest.raises(TechShareAuthError):
        s.reauthenticate()
    refusal = session_module.relogin_refusal()
    assert "\n" not in refusal
    assert "Login rejected by server" in refusal and "Password does not match" in refusal


def test_a_working_re_login_is_untouched():
    s, calls = _session(None)
    s.reauthenticate()
    s.reauthenticate()
    assert calls["login"] == 2
    assert session_module.relogin_refusal() is None


# --------------------------------------------------- a whole fetch loop

def _item(name: str) -> DMEItem:
    return DMEItem(name=name, type="Video - BodyCam", source="Government", size="1 KB",
                   available_date="6/16/2026", last_accessed_date=None, is_archived=False,
                   enclosure_href="https://x/dmefile?dmeId=abc&isStream=1", api_href=None)


class _DeadSessionWithRealRelogin:
    """Every prep fails (a dead session); re-login goes through the REAL
    TechShareSession.reauthenticate, whose login TechShare refuses."""

    def __init__(self, login_error):
        self.inner, self.calls = _session(login_error)
        self.prep_calls = 0

    def prep_dme_download(self, service_id, dme_url):
        self.prep_calls += 1
        raise RuntimeError("prep returned no downloadLink/DefensePortalAuth (status 500)")

    def prepared_download_to_path(self, link, auth, target_path):  # pragma: no cover
        raise AssertionError("never reached")

    def reauthenticate(self):
        self.inner.reauthenticate()


def test_many_failing_files_cost_one_refused_sign_in(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("voxhora_techshare_agent.proxy_client.time.sleep", lambda s: None)
    client = TechShareClient.__new__(TechShareClient)
    client.session = _DeadSessionWithRealRelogin(TechShareAuthError(MISMATCH))
    for n in range(5):
        with pytest.raises(RuntimeError, match="download failed"):
            client.download_dme_file_to_path("svc", _item(f"f{n}.mp4"), tmp_path / f"f{n}.mp4")
    # 5 files × 3 attempts still try to download (the session may still work)…
    assert client.session.prep_calls == 15
    # …but only ONE sign-in reached TechShare (it used to be 2 per file = 10).
    assert client.session.calls["login"] == 1


def test_a_collision_still_re_logs_in_on_every_retry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("voxhora_techshare_agent.proxy_client.time.sleep", lambda s: None)
    client = TechShareClient.__new__(TechShareClient)
    client.session = _DeadSessionWithRealRelogin(TechShareAuthError(COLLISION_USER))
    for n in range(2):
        with pytest.raises(RuntimeError, match="download failed"):
            client.download_dme_file_to_path("svc", _item(f"f{n}.mp4"), tmp_path / f"f{n}.mp4")
    assert client.session.calls["login"] == 4  # today's behaviour: 2 per failing file


class _PCClient:
    """Just enough of TechShareClient for _refresh_one_case: every PC
    download fails (a dead session); re-login is the real one, refused."""

    def __init__(self, session):
        self.session = session
        self.downloads = 0

    def get_case_detail(self, service_id, case_uuid):
        return object()

    def get_dme_list(self, service_id, case):
        return [_item("pc-a.pdf"), _item("pc-b.pdf")]

    def pc_affidavits_in(self, items):
        return list(items)

    def download_dme_file(self, service_id, item):
        self.downloads += 1
        raise RuntimeError("401 Unauthorized")

    def get_plea_offer(self, service_id, case):
        return None


def test_a_refresh_over_many_cases_costs_one_refused_sign_in(monkeypatch):
    # backfill-all / refresh: 3 cases × 2 PCs, one session, TechShare refuses.
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: {"service_id": "s", "case_uuid": "u"})
    monkeypatch.setattr(cli.storage, "load_seen_dme_ids", lambda: set())
    monkeypatch.setattr(cli.storage, "save_seen_dme_ids", lambda seen: set())
    s, calls = _session(TechShareAuthError(MISMATCH))
    client = _PCClient(s)
    for cause in ("CAUSE-A", "CAUSE-B", "CAUSE-C"):
        result = cli._refresh_one_case(cause, client)
        assert result["error"].startswith("PC download failed")
    assert client.downloads == 6, "every PC is still tried once"
    assert calls["login"] == 1, "it used to be one sign-in per PC = 6"


# --------------------------------------------------------- the report

def test_main_reports_the_refusal_in_the_apps_words(monkeypatch, capsys):
    def fake_status(_args):
        s, _ = _session(TechShareAuthError(MISMATCH))
        with pytest.raises(TechShareAuthError):
            s.reauthenticate()
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    rc = cli.main(["status"])
    assert rc == 0, "the run's own result is unchanged (it finished)"
    err = capsys.readouterr().err
    line = "PASSWORD REFUSED: mid-run re-login failed: " + MISMATCH
    assert line in err
    # What the Mac app reads: TechShare's own words after its anchor, on the
    # same stderr line.
    printed = [ln for ln in err.splitlines() if ln.startswith("PASSWORD REFUSED: ")]
    assert len(printed) == 1
    assert "Login rejected by server (400)" in printed[0] and "Password does not match" in printed[0]


@pytest.mark.parametrize("crash, code", [(TechShareAuthError("later sign-in trouble"), 2),
                                         (RuntimeError("something else broke"), 1)])
def test_a_run_that_fails_after_the_refusal_still_reports_it(monkeypatch, capsys, crash, code):
    def fake_status(_args):
        s, _ = _session(TechShareAuthError(MISMATCH))
        with pytest.raises(TechShareAuthError):
            s.reauthenticate()
        raise crash

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    assert cli.main(["status"]) == code
    assert "PASSWORD REFUSED: mid-run re-login failed: " + MISMATCH in capsys.readouterr().err


def test_main_reports_an_expired_password_right_after_failed_(monkeypatch, capsys):
    def fake_status(_args):
        s, _ = _session(TechShareAuthError(EXPIRED))
        with pytest.raises(TechShareAuthError):
            s.reauthenticate()
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    cli.main(["status"])
    err = capsys.readouterr().err
    # The app reads an expired password only right after one of the agent's
    # prefixes ("failed: " here).
    assert "mid-run re-login failed: TechShare password is expired." in err


def test_reporting_never_changes_the_runs_result(monkeypatch):
    class _BrokenStderr:
        def write(self, _text):
            raise OSError("stderr is gone")

        def flush(self):
            pass

    def fake_status(_args):
        s, _ = _session(TechShareAuthError(MISMATCH))
        with pytest.raises(TechShareAuthError):
            s.reauthenticate()
        monkeypatch.setattr(cli.sys, "stderr", _BrokenStderr())  # gone by the end of the run
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    assert cli.main(["status"]) == 0


def test_a_run_without_a_refusal_reports_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_status", lambda _a: 0)
    cli.main(["status"])
    assert "PASSWORD REFUSED" not in capsys.readouterr().err


def test_each_run_starts_clean(monkeypatch, capsys):
    session_module._relogin_refusal = MISMATCH  # left over from an earlier run in this process
    monkeypatch.setattr(cli, "cmd_status", lambda _a: 0)
    cli.main(["status"])
    assert "PASSWORD REFUSED" not in capsys.readouterr().err
