"""cmd_process_email "unknown" emails: registration-only cache heal (2026-08-17).

The 08-15 "Attorney of record" emails named both Rodriguez Vasquez causes and
were discarded while the proactive add-cause at case creation had died (exit 1)
— so nothing ever cached the causes, and every later refresh skipped with
"not in cache". Unknown emails must now attempt a REGISTRATION-ONLY resolve
(cache the cause's TechShare id; never list DME, never download), and must
never fail the email — exit 0 keeps the caller green-flagging, no retry loops.
"""

from __future__ import annotations

import argparse
import io

import pytest

from voxhora_techshare_agent import cli


UNKNOWN_WITH_CAUSE = """Hi Richard,

You have been made Attorney of record for the following case.
Case Number: C1CR26209777
Court: COUNTY COURT AT LAW NO. 4
"""

UNKNOWN_NO_CAUSE = """Hi Richard,

Your TechShare password will expire in 14 days.
"""


class _SessionSpy:
    constructed = 0

    def __init__(self):
        type(self).constructed += 1

    def ensure_authenticated(self):
        return None


@pytest.fixture(autouse=True)
def _quiet_run_log(monkeypatch):
    monkeypatch.setattr(cli.storage, "record_run_result", lambda **kw: None)


def _run(body, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))
    return cli.cmd_process_email(argparse.Namespace(subject="test-subject"))


def test_unknown_email_with_cause_registers_it_without_downloading(monkeypatch):
    _SessionSpy.constructed = 0
    registered = []
    monkeypatch.setattr(cli, "TechShareSession", _SessionSpy)
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: None)
    monkeypatch.setattr(
        cli, "_resolve_and_cache_cause",
        lambda cause, session: registered.append(cause) or {"service_id": 1030, "case_uuid": "u"},
    )
    # Building a TechShareClient means someone is about to list/download —
    # forbidden for unknown emails.
    monkeypatch.setattr(
        cli, "TechShareClient",
        lambda session: (_ for _ in ()).throw(AssertionError("no client for unknown emails")),
    )

    rc = _run(UNKNOWN_WITH_CAUSE, monkeypatch)

    assert rc == 0, "unknown emails must stay exit 0 so the caller green-flags"
    assert registered == ["C1CR26209777"], "the salvaged cause is registered (cache-only)"


def test_unknown_email_without_cause_touches_nothing(monkeypatch):
    _SessionSpy.constructed = 0
    monkeypatch.setattr(cli, "TechShareSession", _SessionSpy)
    called = []
    monkeypatch.setattr(cli, "_resolve_and_cache_cause", lambda cause, session: called.append(cause))

    rc = _run(UNKNOWN_NO_CAUSE, monkeypatch)

    assert rc == 0
    assert _SessionSpy.constructed == 0, "no TechShare session for a cause-less unknown email"
    assert called == []


def test_unknown_email_registration_failure_still_exits_zero(monkeypatch):
    monkeypatch.setattr(cli, "TechShareSession", _SessionSpy)
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: None)

    def boom(cause, session):
        raise RuntimeError("techshare down")

    monkeypatch.setattr(cli, "_resolve_and_cache_cause", boom)

    rc = _run(UNKNOWN_WITH_CAUSE, monkeypatch)
    assert rc == 0, "a failed registration must never bounce the email into a retry loop"
