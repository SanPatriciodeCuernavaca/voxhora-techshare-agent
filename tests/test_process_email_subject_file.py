"""process-email --subject-file (2026-10-11).

County subjects carry defendant names ("Attorney of record in case # …
(DOE, JANE Q)"). Passed as --subject they sat on the command line, where
any user on the Mac can read them with `ps`. Voxhora now writes the subject
to a file in its private per-scan folder and passes --subject-file; the
old --subject stays so an app that hasn't updated yet keeps working.
"""

from __future__ import annotations

import io

import pytest

from voxhora_techshare_agent import cli
from voxhora_techshare_agent.email_parser import parse_email_body

AOR_SUBJECT = "Attorney of record in case # D1DC26309991 (PEÑA, JOSÉ Q)"
AOR_BODY = """Hi Richard,

You have been made Attorney of record for the following case.
Case Number: D1DC26309991
Court: 999TH DISTRICT COURT
"""


class _Parsed(BaseException):
    """Stops cmd_process_email right after parsing (main() catches Exception)."""


@pytest.fixture
def seen(monkeypatch):
    captured: dict = {}

    def capture(body, subject=None):
        captured["body"] = body
        captured["subject"] = subject
        raise _Parsed

    monkeypatch.setattr(cli, "parse_email_body", capture)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(AOR_BODY))
    return captured


def test_subject_file_reaches_the_parser_exactly(seen, tmp_path):
    subject_file = tmp_path / "email.subject.txt"
    subject_file.write_bytes(AOR_SUBJECT.encode("utf-8"))  # AppleScript «class utf8»: no BOM, no newline
    with pytest.raises(_Parsed):
        cli.main(["process-email", "--subject-file", str(subject_file)])
    assert seen["subject"] == AOR_SUBJECT
    event = parse_email_body(seen["body"], subject=seen["subject"])
    assert event.event_type == "attorney_of_record"
    assert event.cause_number == "D1DC26309991"


def test_only_a_trailing_newline_is_dropped(seen, tmp_path):
    subject_file = tmp_path / "email.subject.txt"
    subject_file.write_text("  Case D1DC26309992  \n", encoding="utf-8")
    with pytest.raises(_Parsed):
        cli.main(["process-email", "--subject-file", str(subject_file)])
    assert seen["subject"] == "  Case D1DC26309992  "


def test_subject_flag_still_works_for_an_app_that_has_not_updated(seen):
    with pytest.raises(_Parsed):
        cli.main(["process-email", "--subject", AOR_SUBJECT])
    assert seen["subject"] == AOR_SUBJECT


def test_both_subject_flags_is_a_usage_error(seen, tmp_path):
    with pytest.raises(SystemExit) as stop:
        cli.main(["process-email", "--subject", "a", "--subject-file", str(tmp_path / "s.txt")])
    assert stop.value.code == 2
    assert "subject" not in seen


def test_a_missing_subject_file_fails_the_email_so_it_retries(seen, tmp_path):
    assert cli.main(["process-email", "--subject-file", str(tmp_path / "gone.txt")]) == 1
    assert "subject" not in seen
