"""A cause TechShare doesn't list for this account (2026-10-19).

The cache never had the cause and the self-heal could not resolve it, so
`fetch` exits 4 as always. Exit 4 has meant any failure to resolve — a 404
(TechShare lists no such case: the client hired private counsel), but also a
401, a 5xx, a timeout or an empty answer. `fetch` now adds one
TECHSHARE_LISTS_NO_SUCH_CASE line, still exit 4, only when the cause-number
lookup says 404, another of this account's cases on the same backend answers
in the same run, and the cause is asked once more and is still a 404. The
Mac's "TechShare doesn't list this case under your name" depends on that
line. No extra sign-in. Made-up causes only.
"""

from __future__ import annotations

import argparse
import ast
import inspect

import pytest
import requests

from voxhora_techshare_agent import cli, config, storage
from voxhora_techshare_agent.session import TechShareAuthError

NEW = "C1CR26000010"        # never cached
LIVE_A = "C1CR26000001"
LIVE_B = "C1CR26000002"
DA_LIVE = "D1DC26000003"
DA_NEW = "D1DC26000011"     # never cached
MARKER = cli.NOT_LISTED_MARKER
OLD_HINT = "not in cause→UUID cache"   # what a Mac without the new reader looks for


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Client Error: for url: https://x/api/proxy", response=resp)


def _found(uuid: str) -> dict:
    return {"collection": {"items": [{"data": [{"name": "case-id", "value": uuid}]}]}}


class _Session:
    """`lookups` maps a cause to its answer, or to a list of answers given
    in turn (the last one repeats): a case id, an HTTP status, "empty",
    "timeout", "csrf" (the real api_post_json's CSRF failure) or "notjson".
    Unknown causes answer 404. Every request must go to the cause's own
    backend."""

    def __init__(self, lookups: dict):
        self.lookups = {k: (list(v) if isinstance(v, list) else v) for k, v in lookups.items()}
        self.relogins = 0
        self.asked: list[str] = []

    def ensure_authenticated(self):
        pass

    def reauthenticate(self):
        self.relogins += 1

    def api_post_json(self, path, body):
        cause = body["Path"].split("caseNumber=")[1]
        service_id, backend, _ = cli._backend_for(cause)
        assert (path, body["externalServiceId"], body["Method"]) == ("/api/proxy", service_id, "GET")
        assert body["Path"] == f"{backend}/CaseByCaseNumber?caseNumber={cause}"
        self.asked.append(cause)
        answer = self.lookups.get(cause, 404)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if answer == "timeout":
            raise requests.Timeout("read timed out")
        if answer == "csrf":
            raise TechShareAuthError("No CSRF token available")
        if answer == "notjson":
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        if answer == "empty":
            return {"collection": {"items": []}}
        if isinstance(answer, int):
            raise _http_error(answer)
        return _found(answer)


class _Client:
    def __init__(self):
        self.detail_calls: list[str] = []

    def get_case_detail(self, service_id, case_uuid):
        self.detail_calls.append((service_id, case_uuid))
        return {"uuid": case_uuid}

    def get_dme_list(self, service_id, case):
        return []

    def pc_affidavits_in(self, items):
        return []

    def get_plea_offer(self, service_id, case):
        return None


@pytest.fixture
def cache():
    entries = {
        LIVE_A: {"case_uuid": "uuid-a", "service_id": "svc-ca", "backend_port": 1030},
        LIVE_B: {"case_uuid": "uuid-b", "service_id": "svc-ca", "backend_port": 1030},
        DA_LIVE: {"case_uuid": "uuid-da", "service_id": "svc-da", "backend_port": 1031},
    }
    for cause, entry in entries.items():
        storage.upsert_case_cache_entry(cause, entry)
    return entries


def _wire(monkeypatch, session, client=None):
    client = client or _Client()
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(cli, "_resolve_case", lambda c: storage.lookup_case(c))
    return client


def _fetch(monkeypatch, session, client=None, cause=NEW, service_id=None, case_uuid=None):
    _wire(monkeypatch, session, client)
    return cli.cmd_fetch(argparse.Namespace(
        cause_number=cause, service_id=service_id, case_uuid=case_uuid,
        item_ids=None, target_dir=None, manifest=None, only="written",
    ))


def _marker_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(MARKER)]


# ------------------------------------------------------------- the marker

def test_no_such_case_while_another_case_answers_prints_the_marker(cache, monkeypatch, capsys, caplog):
    session = _Session({NEW: 404, LIVE_A: "uuid-a", LIVE_B: "uuid-b"})
    assert _fetch(monkeypatch, session) == 4, "still exit 4: old Macs read it as today"
    assert _marker_lines(capsys.readouterr().err) == [f"{MARKER}: {NEW}"]
    # main() pipes log lines to stderr (test_main_prints_one_marker_and_exits_4).
    assert OLD_HINT in caplog.text, "today's line too: an old Mac still shows its hint"
    # Controls are asked newest-added first (LIVE_B): one is enough.
    assert session.asked == [NEW, NEW, LIVE_B, NEW], "self-heal, the lookup, a control, then once more"
    assert session.relogins == 0, "no extra sign-in"
    assert storage.lookup_case(NEW) is None, "nothing cached"


def test_a_lowercase_cause_is_asked_and_named_in_capitals(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session, cause="c1cr26000010") == 4
    assert _marker_lines(capsys.readouterr().err) == [f"{MARKER}: {NEW}"]


def test_a_dashed_cause_is_asked_undashed_and_named_undashed(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session, cause="C-1-CR-26-000010") == 4
    assert _marker_lines(capsys.readouterr().err) == [f"{MARKER}: {NEW}"]
    assert session.asked[-2:] == [LIVE_A, NEW]


def test_main_prints_one_marker_and_exits_4(cache, monkeypatch, capsys):
    _wire(monkeypatch, _Session({NEW: 404, LIVE_A: "uuid-a"}))
    assert cli.main(["fetch", NEW, "--only", "written"]) == 4
    err = capsys.readouterr().err
    assert _marker_lines(err) == [f"{MARKER}: {NEW}"]
    assert OLD_HINT in err


def test_the_marker_never_reads_like_the_case_gone_line(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    _fetch(monkeypatch, session)
    err = capsys.readouterr().err
    assert "CASE NOT IN YOUR TECHSHARE" not in err
    assert "CASE NOT IN YOUR TECHSHARE" not in MARKER


def test_a_second_control_is_asked_when_the_first_does_not_answer(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_B: 503, LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == [f"{MARKER}: {NEW}"]


# ------------------------------------------------------------- never the marker

@pytest.mark.parametrize("answer", [401, 403, 500, 503, "timeout", "empty", "csrf", "notjson"])
def test_anything_but_a_404_is_never_the_marker(cache, monkeypatch, capsys, caplog, answer):
    session = _Session({NEW: answer, LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session) == 4, "today's exit 4 — never AUTH ERROR, never a crash"
    assert _marker_lines(capsys.readouterr().err) == []
    assert OLD_HINT in caplog.text


@pytest.mark.parametrize("answer", ["csrf", "notjson", 401, 503])
def test_a_control_with_no_answer_never_vouches(cache, monkeypatch, capsys, answer):
    session = _Session({NEW: 404, LIVE_A: answer, LIVE_B: answer})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []


def test_a_control_that_times_out_ends_the_asking(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_B: "timeout", LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []
    assert session.asked == [NEW, NEW, LIVE_B], "a slow TechShare must not hold the slot for minutes"


def test_no_other_case_answering_is_never_the_marker(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: 503, LIVE_B: 404})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []


def test_no_other_case_to_ask_is_never_the_marker(monkeypatch, capsys):
    session = _Session({NEW: 404})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []
    assert session.asked == [NEW, NEW]


def test_a_control_on_the_other_backend_does_not_count(monkeypatch, capsys):
    storage.upsert_case_cache_entry(DA_LIVE, {"case_uuid": "uuid-da", "service_id": "svc-da", "backend_port": 1031})
    session = _Session({NEW: 404, DA_LIVE: "uuid-da"})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []
    assert DA_LIVE not in session.asked, "a felony case can't vouch for the County Attorney's backend"


@pytest.mark.parametrize("again", [503, "timeout", "empty"])
def test_a_trouble_answer_on_the_second_ask_is_never_the_marker(cache, monkeypatch, capsys, again):
    # "empty" included: kept "unknown" (Gate 2), like the exit-5 check.
    session = _Session({NEW: [404, 404, again], LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session) == 4
    assert _marker_lines(capsys.readouterr().err) == []


def test_a_cached_cause_never_asks_and_never_marks(cache, monkeypatch, capsys):
    session = _Session({LIVE_A: 404, LIVE_B: "uuid-b"})
    client = _Client()
    assert _fetch(monkeypatch, session, client, cause=LIVE_A) == 0
    assert session.asked == []
    assert client.detail_calls == [("svc-ca", "uuid-a")]
    assert _marker_lines(capsys.readouterr().err) == []


@pytest.mark.parametrize("given", [{"service_id": "svc-ca"}, {"case_uuid": "uuid-given"}])
def test_one_id_given_on_the_command_line_never_marks(cache, monkeypatch, capsys, given):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session, **given) == 4
    assert session.asked == [NEW], "the self-heal only"
    assert _marker_lines(capsys.readouterr().err) == []


def test_an_unknown_cause_prefix_is_never_the_marker(cache, monkeypatch, capsys):
    session = _Session({LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session, cause="J1JV26000010") == 4
    assert _marker_lines(capsys.readouterr().err) == []


# ------------------------------------------------------------- found after all

CA_ENTRY = {"case_uuid": "uuid-new", "service_id": config.SERVICE_TRAVIS_COUNTY_ATTORNEY, "backend_port": 1030}


def test_found_on_the_lookup_is_cached_and_the_fetch_continues(cache, monkeypatch, capsys):
    session = _Session({NEW: [404, "uuid-new"], LIVE_A: "uuid-a"})
    client = _Client()
    assert _fetch(monkeypatch, session, client) == 0
    assert client.detail_calls == [(config.SERVICE_TRAVIS_COUNTY_ATTORNEY, "uuid-new")]
    assert storage.lookup_case(NEW) == CA_ENTRY
    assert _marker_lines(capsys.readouterr().err) == []


def test_found_on_the_second_ask_is_cached_and_the_fetch_continues(cache, monkeypatch, capsys):
    session = _Session({NEW: [404, 404, "uuid-new"], LIVE_A: "uuid-a"})
    client = _Client()
    assert _fetch(monkeypatch, session, client) == 0
    assert client.detail_calls == [(config.SERVICE_TRAVIS_COUNTY_ATTORNEY, "uuid-new")]
    assert storage.lookup_case(NEW) == CA_ENTRY


def test_a_felony_found_is_cached_on_the_district_attorneys_backend(cache, monkeypatch, capsys):
    session = _Session({DA_NEW: [404, "uuid-da-new"], DA_LIVE: "uuid-da"})
    client = _Client()
    assert _fetch(monkeypatch, session, client, cause=DA_NEW) == 0
    assert storage.lookup_case(DA_NEW) == {
        "case_uuid": "uuid-da-new", "service_id": config.SERVICE_TRAVIS_DISTRICT_ATTORNEY, "backend_port": 1031}
    assert client.detail_calls == [(config.SERVICE_TRAVIS_DISTRICT_ATTORNEY, "uuid-da-new")]


def test_a_felony_not_listed_is_vouched_for_by_a_felony_case(cache, monkeypatch, capsys):
    session = _Session({DA_NEW: 404, DA_LIVE: "uuid-da", LIVE_A: "uuid-a"})
    assert _fetch(monkeypatch, session, cause=DA_NEW) == 4
    assert _marker_lines(capsys.readouterr().err) == [f"{MARKER}: {DA_NEW}"]
    assert LIVE_A not in session.asked


# ------------------------------------------------------------- sign-in and scope

def test_a_refused_password_at_sign_in_is_still_a_password_refusal(cache, monkeypatch, capsys):
    class Refusing(_Session):
        def ensure_authenticated(self):
            raise TechShareAuthError('Login rejected by server (400): {"password":["Password does not match."]}')
    session = Refusing({NEW: 404, LIVE_A: "uuid-a"})
    _wire(monkeypatch, session)
    assert cli.main(["fetch", NEW, "--only", "written"]) == 2
    err = capsys.readouterr().err
    assert "AUTH ERROR" in err and "Password does not match" in err
    assert _marker_lines(err) == []
    assert session.asked == [], "no lookups without a sign-in"


def test_a_cause_given_with_its_case_id_never_looks_it_up(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    client = _Client()
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(cli, "_resolve_case", lambda c: None)
    assert cli.cmd_fetch(argparse.Namespace(
        cause_number=NEW, service_id="svc-ca", case_uuid="uuid-given",
        item_ids=None, target_dir=None, manifest=None, only="written",
    )) == 0
    assert session.asked == []
    assert _marker_lines(capsys.readouterr().err) == []


def test_fetch_items_keeps_its_exit_4_and_never_asks(cache, monkeypatch, capsys):
    session = _Session({NEW: 404, LIVE_A: "uuid-a"})
    _wire(monkeypatch, session)
    assert cli.cmd_fetch_items(argparse.Namespace(
        cause_number=NEW, service_id=None, case_uuid=None,
        item_ids=["dmeId:x"], target_dir=None, manifest=None,
    )) == 4
    assert session.asked == []
    assert _marker_lines(capsys.readouterr().err) == []


def test_only_fetch_can_print_the_marker():
    """The marker is built in one place and printed from cmd_fetch only
    (read from the code itself, not its text: comments don't count)."""
    tree = ast.parse(inspect.getsource(cli))
    callers, marker_users = set(), set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_not_listed_line":
                callers.add(fn.name)
            if isinstance(node, ast.Name) and node.id == "NOT_LISTED_MARKER":
                marker_users.add(fn.name)
    assert callers == {"cmd_fetch"}
    assert marker_users == {"_not_listed_line"}


def test_the_self_heal_process_email_shares_is_unchanged():
    assert list(inspect.signature(cli._resolve_and_cache_cause).parameters) == ["cause", "session"]
