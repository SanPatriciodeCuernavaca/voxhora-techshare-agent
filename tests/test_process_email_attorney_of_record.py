"""cmd_process_email "Attorney of record" emails: fetch PC + plea (2026-09-25).

The add-cause at case creation runs before the county posts the case, so
TechShare answers 404 (48 of 57 in September 2026) and nothing retried. The
"Attorney of record in case # …" notice arrives hours later, once the case IS
posted, but only cached the cause ("no fetch") — PCs waited for a manual
backfill. It now resolves the cause and runs the light refresh (PC + plea),
and never fails the email (exit 0; the app's daily backfill is the retry).
"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from voxhora_techshare_agent import cli, storage
from voxhora_techshare_agent.email_parser import parse_email_body
from voxhora_techshare_agent.models import DMEItem, PleaOffer

_REAL_RECORD_RUN_RESULT = storage.record_run_result


AOR_SUBJECT = "Attorney of record in case # D1DC26309991 (DOE, JANE Q)"
AOR_BODY = """Hi Richard,

You have been made Attorney of record for the following case.
Case Number: D1DC26309991
Court: 999TH DISTRICT COURT
"""

DISPOSED_SUBJECT = "Case Number D1DC26309992 (ROE, RICHARD) - A Case has been Disposed"
DISPOSED_BODY = """Hi Richard,

Case D1DC26309992, on which you are attorney of record, has been disposed.
"""


# ----- parser -----


def test_attorney_of_record_subject_parses_as_its_own_type() -> None:
    event = parse_email_body(AOR_BODY, subject=AOR_SUBJECT)
    assert event.event_type == "attorney_of_record"
    assert event.cause_number == "D1DC26309991"


def test_attorney_of_record_prefers_the_subject_cause_over_the_body() -> None:
    body = "Hi Richard,\n\nIncident C1CR26200001 relates to this case.\n"
    event = parse_email_body(body, subject="Attorney of record in case # C1CR26200002 (DOE, JANE)")
    assert event.event_type == "attorney_of_record"
    assert event.cause_number == "C1CR26200002"


def test_attorney_of_record_falls_back_to_the_body_cause() -> None:
    event = parse_email_body(AOR_BODY, subject="Attorney of record in case")
    assert event.event_type == "attorney_of_record"
    assert event.cause_number == "D1DC26309991"


def test_body_mention_alone_is_not_an_attorney_of_record_notice() -> None:
    # Only the subject's fixed wording counts — a disposed notice that says
    # "attorney of record" in its body must stay registration-only.
    assert parse_email_body(DISPOSED_BODY, subject=DISPOSED_SUBJECT).event_type == "unknown"
    assert parse_email_body(AOR_BODY, subject="test-subject").event_type == "unknown"
    assert parse_email_body(AOR_BODY).event_type == "unknown"


# The TechShare subject families seen Jun–Sep 2026 other than DME and plea
# (those match on the body first); names/causes replaced.
@pytest.mark.parametrize("subject,expected", [
    ("Attorney of record in case # C1CR26200003 (DOE, JANE)", "attorney_of_record"),
    ("Attorney of record in case # C1CR26200003  (DOE, JOHN ALLEN, Jr)", "attorney_of_record"),
    ("[EXTERNAL] Attorney of record in case # C1CR26200003 (DOE, JANE)", "attorney_of_record"),
    ("FW: Attorney of record in case # C1CR26200003 (DOE, JANE)", "attorney_of_record"),
    ("Case Number C1CR26200003 (DOE, JANE) - A Case has been Disposed", "unknown"),
    ("Case Number C1CR26200003 (DOE, JANE) - Discovery Notes Updated", "unknown"),
    ("Case#: C1CR26200003 (DOE, JANE): Charge Rejected", "unknown"),
    ("Case# C1CR26200003 (DOE, JANE):  mitigation info pending", "unknown"),
])
def test_real_subject_shapes(subject, expected) -> None:
    body = "Hi Richard,\n\nCase Number: C1CR26200003\n"
    assert parse_email_body(body, subject=subject).event_type == expected


# ----- cmd_process_email -----


class _SessionSpy:
    constructed = 0

    def __init__(self):
        type(self).constructed += 1

    def ensure_authenticated(self):
        return None


class _ClientSpy:
    def __init__(self, session):
        self.session = session


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(cli, "_ATTORNEY_OF_RECORD_PACING_SECONDS", 0)
    _SessionSpy.constructed = 0
    runs = []
    monkeypatch.setattr(cli.storage, "record_run_result", lambda **kw: runs.append(kw))
    monkeypatch.setattr(cli, "TechShareSession", _SessionSpy)
    monkeypatch.setattr(cli, "TechShareClient", _ClientSpy)
    return runs


def _run(body, subject, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))
    return cli.cmd_process_email(argparse.Namespace(subject=subject))


def test_attorney_of_record_resolves_then_fetches_pc_and_plea(monkeypatch, _isolate):
    registered, refreshed = [], []
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: None)
    monkeypatch.setattr(
        cli, "_resolve_and_cache_cause",
        lambda cause, session: registered.append(cause) or {"service_id": 1031, "case_uuid": "u"},
    )

    def fake_refresh(cause, client, force_pc=False):
        refreshed.append((cause, force_pc, type(client)))
        return {"cause": cause, "pcs_downloaded": 1, "plea_captured": True, "skipped": False, "error": None}

    monkeypatch.setattr(cli, "_refresh_one_case", fake_refresh)

    rc = _run(AOR_BODY, AOR_SUBJECT, monkeypatch)

    assert rc == 0
    assert registered == ["D1DC26309991"]
    assert refreshed == [("D1DC26309991", False, _ClientSpy)], "light refresh, seen-set respected (no --force-pc)"
    assert _isolate[-1]["event_type"] == "attorney_of_record"
    assert _isolate[-1]["pcs_downloaded"] == 1 and _isolate[-1]["plea_captured"] is True


def test_attorney_of_record_for_an_already_cached_cause_still_fetches(monkeypatch):
    refreshed = []
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: {"service_id": 1031, "case_uuid": "u"})
    monkeypatch.setattr(
        cli, "_resolve_and_cache_cause",
        lambda cause, session: (_ for _ in ()).throw(AssertionError("cached cause must not re-resolve")),
    )
    monkeypatch.setattr(
        cli, "_refresh_one_case",
        lambda cause, client, force_pc=False: refreshed.append(cause)
        or {"cause": cause, "pcs_downloaded": 0, "plea_captured": False, "skipped": False, "error": None},
    )

    assert _run(AOR_BODY, AOR_SUBJECT, monkeypatch) == 0
    assert refreshed == ["D1DC26309991"]


def test_attorney_of_record_not_on_techshare_yet_fetches_nothing(monkeypatch, _isolate):
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: None)
    monkeypatch.setattr(cli, "_resolve_and_cache_cause", lambda cause, session: None)
    monkeypatch.setattr(
        cli, "_refresh_one_case",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh for an unresolvable case")),
    )

    assert _run(AOR_BODY, AOR_SUBJECT, monkeypatch) == 0
    assert _isolate[-1]["error"] == "not_resolvable"


def test_attorney_of_record_failure_still_exits_zero(monkeypatch, _isolate):
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: {"service_id": 1031, "case_uuid": "u"})

    def boom(*a, **k):
        raise RuntimeError("techshare down")

    monkeypatch.setattr(cli, "_refresh_one_case", boom)

    assert _run(AOR_BODY, AOR_SUBJECT, monkeypatch) == 0, "a failed fetch must never bounce the email into a retry loop"
    assert "techshare down" in _isolate[-1]["error"]


def test_attorney_of_record_without_any_cause_touches_nothing(monkeypatch):
    assert _run("Hi Richard,\n\nNothing here.\n", "Attorney of record in case", monkeypatch) == 0
    assert _SessionSpy.constructed == 0


def test_disposed_notice_stays_registration_only(monkeypatch, _isolate):
    registered, refreshed = [], []
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: None)
    monkeypatch.setattr(
        cli, "_resolve_and_cache_cause",
        lambda cause, session: registered.append(cause) or {"service_id": 1031, "case_uuid": "u"},
    )
    # Recorded, not raised: the AOR handler catches exceptions, so a raising
    # spy would pass even if a disposed notice were routed there.
    monkeypatch.setattr(cli, "_refresh_one_case", lambda *a, **k: refreshed.append(a))

    assert _run(DISPOSED_BODY, DISPOSED_SUBJECT, monkeypatch) == 0
    assert registered == ["D1DC26309992"]
    assert refreshed == [], "no download for a disposed notice"
    assert _isolate[-1]["event_type"] == "unknown"


def test_login_failure_is_recorded_not_retried(monkeypatch, _isolate):
    # A non-zero exit re-runs the email every Mail tick: with a wrong password
    # that is one failed TechShare login per notice every 5 minutes (lockout
    # risk). Record it and exit 0; the daily backfill retries the case.
    class _AuthFailingSession(_SessionSpy):
        def ensure_authenticated(self):
            raise cli.TechShareAuthError("bad credentials")

    monkeypatch.setattr(cli, "TechShareSession", _AuthFailingSession)

    assert _run(AOR_BODY, AOR_SUBJECT, monkeypatch) == 0
    assert "bad credentials" in _isolate[-1]["error"]


def test_run_log_failure_still_exits_zero(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: {"service_id": 1031, "case_uuid": "u"})
    monkeypatch.setattr(
        cli, "_refresh_one_case",
        lambda cause, client, force_pc=False:
        {"cause": cause, "pcs_downloaded": 1, "plea_captured": False, "skipped": False, "error": None},
    )

    def disk_full(**kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(cli.storage, "record_run_result", disk_full)

    assert _run(AOR_BODY, AOR_SUBJECT, monkeypatch) == 0, "a run-log hiccup must not re-run the fetch every tick"


# ----- end to end: real resolve, real refresh, real storage (temp dirs) -----


class _FakeTechShareSession:
    def ensure_authenticated(self):
        return None

    def reauthenticate(self):
        return None

    def api_post_json(self, path, body):
        assert path == "/api/proxy"
        assert "CaseByCaseNumber?caseNumber=C1CR26200003" in body["Path"]
        return {"collection": {"items": [{"data": [{"name": "case-id", "value": "uuid-e2e"}]}]}}


_E2E_PC = DMEItem(
    name="C1CR26200003.pdf",
    type="PC Affidavit / Arrest Warrant",
    source="Government",
    size="120 KB",
    available_date="9/23/2026",
    last_accessed_date=None,
    is_archived=False,
    enclosure_href="/dmefile?dmeId=11111111-2222-3333-4444-555555555555&isStream=false",
    api_href=None,
)


class _FakeTechShareClient:
    def __init__(self, session):
        self.session = session

    def get_case_detail(self, service_id, case_uuid):
        assert case_uuid == "uuid-e2e", "the refresh must use the entry the resolve just cached"
        return object()

    def get_dme_list(self, service_id, case):
        return [_E2E_PC]

    def pc_affidavits_in(self, items):
        return [i for i in items if i.is_pc_affidavit]

    def download_dme_file(self, service_id, item):
        return b"%PDF-1.4 e2e"

    def get_plea_offer(self, service_id, case):
        return PleaOffer(text="9/24/2026\r\n[Count 1 - A001] 10 days", can_sign=False,
                         has_document=False, actions_id=None)


def test_end_to_end_fetch_lands_pc_and_plea_in_the_inbox(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("VOXHORA_DROPBOX_INBOX", str(inbox))
    for var in ("VOXHORA_TECHSHARE_CASE_UUID", "VOXHORA_TECHSHARE_SERVICE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.storage, "record_run_result", _REAL_RECORD_RUN_RESULT)
    monkeypatch.setattr(cli, "TechShareSession", _FakeTechShareSession)
    monkeypatch.setattr(cli, "TechShareClient", _FakeTechShareClient)

    subject = "Attorney of record in case # C1CR26200003 (DOE, JANE)"
    body = "Hi Richard,\n\nCase Number: C1CR26200003\n"
    assert _run(body, subject, monkeypatch) == 0

    assert storage.lookup_case("C1CR26200003")["case_uuid"] == "uuid-e2e"
    assert (inbox / "C1CR26200003.pdf").read_bytes() == b"%PDF-1.4 e2e"
    assert "10 days" in (inbox / "C1CR26200003.plea.json").read_text()
    last = json.loads(cli.config.last_run_path().read_text())["history"][-1]
    assert last["event_type"] == "attorney_of_record"
    assert last["pcs_downloaded"] == 1 and last["plea_captured"] is True and last["error"] is None
