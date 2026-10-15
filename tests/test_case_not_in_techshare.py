"""A case that has left the attorney's TechShare (2026-10-15).

For such a case TechShare answers the case-detail call with a 500 — which
reads exactly like TechShare being down — and the cause-number lookup with a
404 (live probe on a stale case, 10-15). `fetch` and `refresh` exit 5 with one
CASE NOT IN YOUR TECHSHARE line only after a fresh sign-in and a retry still
fail, the lookup has no such case, AND TechShare answers for another of this
account's cases on the same backend in the same run (Patrick: "other cases
worked with the same sign-in, so TechShare itself was up"). Everything else
stays today's error (exit 1). Made-up causes only.
"""

from __future__ import annotations

import argparse
import json

import pytest
import requests

from voxhora_techshare_agent import cli, config, storage
from voxhora_techshare_agent.session import TechShareAuthError

GONE = "C1CR26000000"
LIVE_A = "C1CR26000001"
LIVE_B = "C1CR26000002"
DA_LIVE = "D1DC26000003"
MISMATCH = ('Login rejected by server (400): {"message":"The request is invalid.","modelState":'
            '{"password":["Password does not match."]}}')


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Server Error: for url: https://x/api/proxy", response=resp)


def _found(uuid: str) -> dict:
    return {"collection": {"items": [{"data": [{"name": "case-id", "value": uuid}]}]}}


class _Session:
    """TechShare as seen by the agent. `lookups` maps a cause to its
    cause-number answer: a case id, 404, "empty" or "error"."""

    def __init__(self, lookups: dict, relogin_error: Exception | None = None):
        self.lookups = lookups
        self.relogin_error = relogin_error
        self.relogins = 0
        self.asked: list[str] = []

    def ensure_authenticated(self):
        pass

    def reauthenticate(self):
        self.relogins += 1
        if self.relogin_error is not None:
            raise self.relogin_error

    def api_post_json(self, path, body):
        cause = body["Path"].split("caseNumber=")[1]
        self.asked.append(cause)
        answer = self.lookups.get(cause, 404)
        if answer == 404:
            raise _http_error(404)
        if answer == "empty":
            return {"collection": {"items": []}}
        if answer == "error":
            raise _http_error(503)
        return _found(answer)


class _Client:
    """Case detail answers by case id: 500 for ids in `broken` (each call
    pops one answer when a list is given), a dict otherwise."""

    def __init__(self, session, broken: dict):
        self.session = session
        self.broken = broken
        self.detail_calls: list[str] = []

    def get_case_detail(self, service_id, case_uuid):
        self.detail_calls.append(case_uuid)
        answers = self.broken.get(case_uuid)
        if isinstance(answers, list):
            answer = answers.pop(0) if answers else None
        else:
            answer = answers
        if answer is not None:
            raise _http_error(answer)
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
        GONE: {"case_uuid": "uuid-gone", "service_id": "svc-ca", "backend_port": 1030},
        LIVE_A: {"case_uuid": "uuid-a", "service_id": "svc-ca", "backend_port": 1030},
        LIVE_B: {"case_uuid": "uuid-b", "service_id": "svc-ca", "backend_port": 1030},
        DA_LIVE: {"case_uuid": "uuid-da", "service_id": "svc-da", "backend_port": 1031},
    }
    for cause, entry in entries.items():
        storage.upsert_case_cache_entry(cause, entry)
    return entries


def _fetch(monkeypatch, session, client, cause=GONE):
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(cli, "_resolve_case", lambda c: storage.lookup_case(c))
    entry = storage.lookup_case(cause)
    return cli.cmd_fetch(argparse.Namespace(
        cause_number=cause, service_id=entry["service_id"], case_uuid=entry["case_uuid"],
        item_ids=None, target_dir=None, manifest=None, only="written",
    ))


# ------------------------------------------------------------- gone

def test_no_such_case_while_another_case_answers_is_exit_5(cache, monkeypatch, capsys):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    client = _Client(session, {"uuid-gone": 500})
    assert _fetch(monkeypatch, session, client) == cli.CASE_GONE_EXIT == 5
    err = capsys.readouterr().err
    assert f"CASE NOT IN YOUR TECHSHARE: TechShare has no case {GONE} for this account" in err
    assert session.relogins == 1, "a fresh sign-in first"
    assert client.detail_calls == ["uuid-gone", "uuid-gone"], "and one retry"
    assert storage.lookup_case(GONE)["case_uuid"] == "uuid-gone", "the cache is left alone"


def test_an_empty_answer_is_never_no_such_case(cache, monkeypatch):
    # Only TechShare's 404 counts (Gate 2: a degraded backend may answer empty).
    session = _Session({GONE: "empty", LIVE_A: "uuid-a"})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


def test_the_case_is_asked_again_after_a_control_answers(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert session.asked[-2:] == [LIVE_A, GONE]


def test_a_case_that_answers_on_the_second_question_is_not_gone(cache, monkeypatch):
    # TechShare comes back between the two questions: never exit 5 (Gate 2).
    answers = iter([404, "uuid-gone"])
    session = _Session({LIVE_A: "uuid-a"})
    original = session.api_post_json

    def flaky(path, body):
        if body["Path"].endswith(GONE):
            session.asked.append(GONE)
            answer = next(answers)
            if answer == 404:
                raise _http_error(404)
            return _found(answer)
        return original(path, body)

    session.api_post_json = flaky
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


# --------------------------------------------- TechShare up? (the controls)

@pytest.mark.parametrize("controls", [
    {LIVE_A: 404, LIVE_B: 404},          # nothing answers for this account
    {LIVE_A: "error", LIVE_B: "error"},  # the backend errors
    {LIVE_A: "empty", LIVE_B: "error"},
    {LIVE_A: "empty", LIVE_B: "empty"},
])
def test_without_a_working_control_it_is_todays_error(cache, monkeypatch, controls):
    session = _Session({GONE: 404, **controls})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))  # main() → exit 1


def test_the_other_backend_is_never_a_control(cache, monkeypatch):
    # Only the District Attorney backend answers; the case is County Attorney.
    storage.upsert_case_cache_entry(LIVE_A, {"case_uuid": "uuid-a", "service_id": "x", "backend_port": 1030})
    session = _Session({GONE: 404, LIVE_A: 404, LIVE_B: 404, DA_LIVE: "uuid-da"})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert DA_LIVE not in session.asked


def test_controls_are_never_the_same_case(cache, monkeypatch):
    # The same case under a dashed key, and another cause cached with the same case id.
    storage.upsert_case_cache_entry("C-1-CR-26-000000", cache[GONE])
    storage.upsert_case_cache_entry("C1CR26000009", {"case_uuid": "uuid-gone", "service_id": "svc-ca", "backend_port": 1030})
    controls = cli._control_causes(GONE, "uuid-gone")
    assert GONE not in controls and "C1CR26000009" not in controls
    assert all(c.startswith("C1CR") for c in controls)


def test_a_dashed_cache_key_still_serves_as_a_control(cache, monkeypatch):
    storage.upsert_case_cache_entry("C-1-CR-26-000007", {"case_uuid": "uuid-7", "service_id": "svc-ca", "backend_port": 1030})
    assert "C1CR26000007" in cli._control_causes(GONE, "uuid-gone", limit=10)


def test_at_most_three_controls_and_newest_working_first(cache, monkeypatch):
    for n in range(10, 16):
        storage.upsert_case_cache_entry(f"C1CR260000{n}", {"case_uuid": f"u{n}", "service_id": "s", "backend_port": 1030})
    storage.record_run_result(mode="refresh", cause_number=LIVE_B, error=None)
    storage.record_run_result(mode="refresh", cause_number="C1CR26000012", error="case-detail failed: x")
    storage.record_run_result(mode="refresh", cause_number=LIVE_A, error=None)
    controls = cli._control_causes(GONE, "uuid-gone")
    assert controls[:2] == [LIVE_A, LIVE_B], "the newest error-free runs first"
    assert len(controls) == 3


def test_it_stops_at_the_first_control_that_answers(cache, monkeypatch):
    storage.record_run_result(mode="refresh", cause_number=LIVE_A, error=None)
    session = _Session({GONE: 404, LIVE_A: "uuid-a", LIVE_B: "uuid-b"})
    _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert session.asked == [GONE, LIVE_A, GONE], "and the case once more"


# ------------------------------------------------- not gone after all

def test_a_fresh_sign_in_heals_it(cache, monkeypatch):
    session = _Session({})
    client = _Client(session, {"uuid-gone": [500]})  # only the first call fails
    assert _fetch(monkeypatch, session, client) == 0
    assert session.asked == [], "no lookup when the retry works"


def test_a_listed_case_is_a_hiccup(cache, monkeypatch):
    session = _Session({GONE: "uuid-gone", LIVE_A: "uuid-a"})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


def test_a_moved_case_id_is_cached_and_used(cache, monkeypatch):
    session = _Session({GONE: "uuid-new"})
    client = _Client(session, {"uuid-gone": 500})
    assert _fetch(monkeypatch, session, client) == 0
    assert client.detail_calls[-1] == "uuid-new"
    assert storage.lookup_case(GONE)["case_uuid"] == "uuid-new"


def test_a_lookup_error_is_todays_error(cache, monkeypatch):
    session = _Session({GONE: "error", LIVE_A: "uuid-a"})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


@pytest.mark.parametrize("status", [401, 404, 503])
def test_only_a_500_starts_the_check(cache, monkeypatch, status):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": status}))
    assert session.relogins == 0 and session.asked == []


def test_a_500_listing_the_files_is_todays_error(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    client = _Client(session, {})

    def broken_list(service_id, case):
        raise _http_error(500)

    client.get_dme_list = broken_list
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, client)
    assert session.relogins == 0


# ------------------------------------------------------- the sign-in

def test_an_outage_on_the_sign_in_is_never_an_auth_error(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"},
                       relogin_error=TechShareAuthError("POST /api/auth returned 503: Service Unavailable"))
    with pytest.raises(requests.HTTPError):  # the ORIGINAL 500 → exit 1, not exit 2
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert session.asked == [], "no lookup without a fresh sign-in"


def test_a_refused_password_stays_a_password_refusal(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"}, relogin_error=TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError, match="Password does not match"):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert session.asked == []


def test_main_maps_it_end_to_end(cache, monkeypatch, capsys):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: _Client(session, {"uuid-gone": 500}))
    assert cli.main(["fetch", GONE, "--only", "written"]) == 5
    session2 = _Session({GONE: "uuid-gone"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: session2)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: _Client(session2, {"uuid-gone": 500}))
    assert cli.main(["fetch", GONE, "--only", "written"]) == 1


# ------------------------------------------------- refresh / backfill-all

def _refresh(monkeypatch, session, client, cause=GONE):
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    return cli.cmd_refresh(argparse.Namespace(cause_number=cause, force_pc=False))


def test_refresh_exits_5_with_the_line(cache, monkeypatch, capsys):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    assert _refresh(monkeypatch, session, _Client(session, {"uuid-gone": 500})) == 5
    assert "CASE NOT IN YOUR TECHSHARE" in capsys.readouterr().err


def test_refresh_without_a_control_is_todays_error(cache, monkeypatch, capsys):
    session = _Session({GONE: 404, LIVE_A: 404, LIVE_B: 404})
    assert _refresh(monkeypatch, session, _Client(session, {"uuid-gone": 500})) == 1
    assert "case-detail failed" in capsys.readouterr().err


def test_refresh_lets_a_refused_password_through(cache, monkeypatch):
    session = _Session({GONE: 404}, relogin_error=TechShareAuthError(MISMATCH))
    with pytest.raises(TechShareAuthError):  # main() → exit 2 + AUTH ERROR
        _refresh(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


def test_backfill_all_names_it_and_keeps_its_exit_code(cache, monkeypatch, capsys):
    session = _Session({GONE: 404, LIVE_A: "uuid-a", LIVE_B: "uuid-b", DA_LIVE: "uuid-da"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: session)
    monkeypatch.setattr(cli, "TechShareClient", lambda s: _Client(session, {"uuid-gone": 500}))
    rc = cli.cmd_backfill_all(argparse.Namespace(limit=0, rate_limit_seconds=0))
    assert rc == 2, "today's code for a run with errors"
    assert "CASE NOT IN YOUR TECHSHARE" in capsys.readouterr().err


# ------------------------------------------- more edges (Gate 2 test review)

def test_a_lookup_500_is_todays_error(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"})
    original = session.api_post_json

    def lookup_500(path, body):
        if body["Path"].endswith(GONE):
            session.asked.append(GONE)
            raise _http_error(500)
        return original(path, body)

    session.api_post_json = lookup_500
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))


def test_an_answer_without_a_case_id_is_no_answer():
    class S:
        def api_post_json(self, path, body):
            return {"collection": {"items": [{"data": [{"name": "case-number", "value": "x"}]}]}}
    assert cli._lookup_cause(GONE, S()) == ("error", None)


def test_a_sign_in_that_cannot_connect_stops_the_check(cache, monkeypatch):
    session = _Session({GONE: 404, LIVE_A: "uuid-a"}, relogin_error=requests.ConnectionError("no route"))
    with pytest.raises(requests.HTTPError):
        _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    assert session.asked == []


def test_a_moved_case_keeps_its_backend(cache, monkeypatch):
    session = _Session({GONE: "uuid-new"})
    _fetch(monkeypatch, session, _Client(session, {"uuid-gone": 500}))
    entry = storage.lookup_case(GONE)
    assert entry["backend_port"] == 1030 and entry["service_id"] == "svc-ca"


def test_controls_are_distinct_and_fall_back_to_the_newest_cached(cache, monkeypatch):
    for _ in range(3):
        storage.record_run_result(mode="refresh", cause_number=LIVE_A, error=None)
    controls = cli._control_causes(GONE, "uuid-gone", limit=10)
    assert controls.count(LIVE_A) == 1
    assert controls == [LIVE_A, LIVE_B], "history first, then the cache newest-added first"
    # No history at all: the cache, newest-added first.
    config.last_run_path().unlink()
    assert cli._control_causes(GONE, "uuid-gone", limit=10) == [LIVE_B, LIVE_A]
