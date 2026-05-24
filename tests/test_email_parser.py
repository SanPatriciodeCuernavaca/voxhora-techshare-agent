"""Tests for the TechShare email parser using real (sanitized) email samples."""

from __future__ import annotations

from pathlib import Path

from voxhora_techshare_agent.email_parser import parse_email_body, extract_all_cause_numbers


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_dme_email_parsed() -> None:
    event = parse_email_body(_load("email_dme.txt"))
    assert event.event_type == "dme_discoverable"
    assert event.cause_number == "C1CR25208232"
    assert event.defendant_name == "WATTS, JARRET JONATHAN"
    assert "Axon_Fleet_3_Front_Camera_Video_2026-01-05_1452.mp4" in event.filenames


def test_plea_offer_email_parsed() -> None:
    event = parse_email_body(_load("email_plea.txt"))
    assert event.event_type == "plea_offer_updated"
    assert event.cause_number == "C1CR26203830"
    assert event.defendant_name is None
    assert event.filenames == ()


def test_unknown_email_returns_unknown_type() -> None:
    body = "Hi Richard,\n\nThis is a TechShare email about something unrecognized.\nCase D1DC23207931 - some unhandled event type."
    event = parse_email_body(body)
    assert event.event_type == "unknown"
    # Should still salvage the cause number for logging
    assert event.cause_number == "D1DC23207931"


def test_cause_number_extraction_handles_both_prefixes() -> None:
    text = "Case C1CR26203830 references D1DC23207931 and another C1CR25208232."
    nums = extract_all_cause_numbers(text)
    assert nums == ["C1CR26203830", "D1DC23207931", "C1CR25208232"]


def test_dme_email_filename_dedup() -> None:
    body = (
        "Hi Richard,\n"
        "Case Number: C1CR99999999\n"
        "The following DME was made discoverable:\n"
        "video1.mp4, video1.mp4, audio.m4a, document.pdf"
    )
    event = parse_email_body(body)
    assert event.event_type == "dme_discoverable"
    # 'video1.mp4' deduplicated; order preserved
    assert event.filenames == ("video1.mp4", "audio.m4a", "document.pdf")
