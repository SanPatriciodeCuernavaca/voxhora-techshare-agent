"""Every PC-type item reaches the inbox (2026-09-24).

Guards the Giovany Vazquez fix (C1CR25501953). TechShare typed three items
"PC Affidavit / Arrest Warrant" for that cause: "Vazquez PC.pdf" (the PC),
"C1CR25501953.pdf" (a second affidavit) and "25-501953 OC.pdf" (the Order of
Commitment). The agent wrote all three to Bulk_Inbox/C1CR25501953.pdf within
half a second. With --force-pc the last write won, so the watcher only ever
saw the commitment order, rejected it for having no PC markers, and the case
never got its PC or synopsis, on every pass for two weeks. Without force the
first write wins instead, and the others are marked seen without ever being
delivered.

Now: one PC item keeps `<cause>.pdf` exactly; two or more each get their own
stable name, and the watcher decides which one is the PC.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import types
from pathlib import Path

import pytest

from voxhora_techshare_agent import cli, config, storage
from voxhora_techshare_agent.models import DMEItem

CAUSE = "C1CR25501953"


def _pc(name: str, dme_id: str | None, size: str = "100 KB") -> DMEItem:
    return DMEItem(
        name=name,
        type="PC Affidavit / Arrest Warrant",
        source="Government",
        size=size,
        available_date="9/10/2026",
        last_accessed_date=None,
        is_archived=False,
        enclosure_href=f"https://example/dmefile?dmeId={dme_id}" if dme_id else None,
        api_href=None,
    )


GIOVANY = [
    _pc("Vazquez PC.pdf", "11111111-1111-1111-1111-111111111111", "103 KB"),
    _pc("C1CR25501953.pdf", "22222222-2222-2222-2222-222222222222", "200 KB"),
    _pc("25-501953 OC.pdf", "33333333-3333-3333-3333-333333333333", "47 KB"),
]


def _watcher_cause(filename: str) -> str | None:
    """The Mac watcher's filename tier, mirrored from
    TravisCountyPCAffidavitDetector.normalizeFilename + canonicalPattern
    (voxhora-ios, Engine/PCAffidavitDetectors). The iOS suite pins the real
    Swift detector against the same names."""
    s = filename
    i = s.lower().rfind(".pdf")
    if i != -1:
        s = s[:i] + s[i + 4:]
    s = s.upper()
    for noise in (" PC", "_PC", "-PC", "PC", "-", ".", " ", "_"):
        s = s.replace(noise, "")
    m = re.search(r"(C1CR|D1DC)\d{8}", s)
    return m.group(0) if m else None


# ----- naming -----


def test_single_pc_keeps_the_plain_cause_name():
    only = GIOVANY[0]
    assert storage.pc_inbox_filename(only, CAUSE, [only]) == f"{CAUSE}.pdf"
    assert storage.pc_inbox_filename(only, CAUSE, []) == f"{CAUSE}.pdf"
    assert storage.pc_inbox_filename(only, CAUSE) == f"{CAUSE}.pdf"


def test_same_item_listed_twice_is_still_one_pc():
    """A duplicated listing of ONE item (same dmeId) is not a sibling."""
    only = GIOVANY[0]
    assert storage.pc_inbox_filename(only, CAUSE, [only, only]) == f"{CAUSE}.pdf"


def test_several_pcs_get_distinct_cause_prefixed_names():
    names = [storage.pc_inbox_filename(it, CAUSE, GIOVANY) for it in GIOVANY]
    assert len(set(n.casefold() for n in names)) == 3  # macOS paths ignore case
    for n in names:
        assert re.fullmatch(rf"{CAUSE} item-[0-9a-f]{{8}}\.pdf", n), n


def test_names_are_stable_across_runs_and_listing_order():
    first = {it.name: storage.pc_inbox_filename(it, CAUSE, GIOVANY) for it in GIOVANY}
    reordered = list(reversed(GIOVANY))
    again = {it.name: storage.pc_inbox_filename(it, CAUSE, reordered) for it in reordered}
    assert first == again


def test_names_never_carry_techshares_item_name():
    """Item names hold the defendant's name; inbox names reach the shared log."""
    for it in GIOVANY:
        name = storage.pc_inbox_filename(it, CAUSE, GIOVANY)
        assert "vazquez" not in name.lower()
        assert "OC" not in name


@pytest.mark.parametrize("cause", [CAUSE, "D1DC26206980", "C1CR23502071"])
def test_the_watcher_reads_the_right_cause_from_every_name(cause):
    names = [storage.pc_inbox_filename(it, cause, GIOVANY) for it in GIOVANY]
    for n in names + [f"{cause}.pdf"]:
        assert _watcher_cause(n) == cause, n


def test_items_without_a_dme_id_are_told_apart_by_name_size_and_date():
    a = _pc("26-160044 PC.pdf", None, "700 KB")
    b = _pc("26-160044 PC(1).pdf", None, "700 KB")
    names = {storage.pc_inbox_filename(it, CAUSE, [a, b]) for it in (a, b)}
    assert len(names) == 2


def test_a_shared_short_id_falls_back_to_the_full_hash(monkeypatch):
    """Two siblings whose first eight hex digits collide must still get
    different names — a shared name is the original bug."""
    real = hashlib.sha256

    def colliding(data=b""):
        digest = real(data).hexdigest()
        return types.SimpleNamespace(hexdigest=lambda: "deadbeef" + digest[8:])

    monkeypatch.setattr(storage, "hashlib", types.SimpleNamespace(sha256=colliding))
    a, b = GIOVANY[0], GIOVANY[1]
    na = storage.pc_inbox_filename(a, CAUSE, [a, b])
    nb = storage.pc_inbox_filename(b, CAUSE, [a, b])
    assert na != nb
    assert re.fullmatch(rf"{CAUSE} item-[0-9a-f]{{64}}\.pdf", na)


# ----- the bug, end to end through the refresh the app runs -----


class _FakeClient:
    """Just enough of TechShareClient for _refresh_one_case."""

    def __init__(self, items, contents):
        self._items = items
        self._contents = contents
        self.session = types.SimpleNamespace(reauthenticate=lambda: None)

    def get_case_detail(self, service_id, case_uuid):
        return object()

    def get_dme_list(self, service_id, case):
        return list(self._items)

    def pc_affidavits_in(self, items):
        return [it for it in items if it.is_pc_affidavit]

    def download_dme_file(self, service_id, item):
        return self._contents[item.name]

    def get_plea_offer(self, service_id, case):
        return None


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    box = tmp_path / "Bulk_Inbox"
    box.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "dropbox_inbox", lambda: box)
    monkeypatch.setattr(config, "state_dir", lambda username=None: state)
    monkeypatch.setattr(cli, "_resolve_case", lambda cause: {"service_id": "s", "case_uuid": "u"})
    return box


CONTENTS = {
    "Vazquez PC.pdf": b"%PDF the probable cause affidavit",
    "C1CR25501953.pdf": b"%PDF affidavit for warrant of arrest",
    "25-501953 OC.pdf": b"%PDF order of commitment",
}


@pytest.mark.parametrize("force_pc", [True, False])
def test_refresh_delivers_every_pc_type_item(inbox, force_pc):
    result = cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=force_pc)

    assert result["error"] is None
    delivered = sorted(p.read_bytes() for p in inbox.glob("*.pdf"))
    assert delivered == sorted(CONTENTS.values())
    assert result["pcs_downloaded"] == 3


def test_forced_refresh_overwrites_only_the_same_items_copy(inbox):
    """A re-fetch replaces each item's own earlier drop, never a sibling's."""
    client = _FakeClient(GIOVANY, CONTENTS)
    cli._refresh_one_case(CAUSE, client, force_pc=True)
    cli._refresh_one_case(CAUSE, client, force_pc=True)
    assert sorted(p.read_bytes() for p in inbox.glob("*.pdf")) == sorted(CONTENTS.values())


def test_single_pc_refresh_still_lands_at_the_plain_name(inbox):
    only = [GIOVANY[0]]
    cli._refresh_one_case(CAUSE, _FakeClient(only, CONTENTS), force_pc=True)
    assert [p.name for p in inbox.glob("*.pdf")] == [f"{CAUSE}.pdf"]
    assert (inbox / f"{CAUSE}.pdf").read_bytes() == CONTENTS["Vazquez PC.pdf"]


def test_the_name_follows_the_techshare_id_not_the_item_name():
    """TechShare can rename an item; its dmeId, and so its inbox name, stays."""
    renamed = _pc("Vazquez PC (corrected).pdf", "11111111-1111-1111-1111-111111111111", "103 KB")
    before = storage.pc_inbox_filename(GIOVANY[0], CAUSE, GIOVANY)
    after = storage.pc_inbox_filename(renamed, CAUSE, [renamed] + GIOVANY[1:])
    assert before == after
    same_name = _pc("Vazquez PC.pdf", "44444444-4444-4444-4444-444444444444", "103 KB")
    assert storage.pc_inbox_filename(same_name, CAUSE, GIOVANY + [same_name]) != before


def test_pc_items_is_required():
    """Forgetting the list must fail loudly, not quietly name every item <cause>.pdf."""
    with pytest.raises(TypeError):
        storage.write_pc_affidavit(GIOVANY[0], b"x", CAUSE)  # type: ignore[call-arg]


# ----- a plain <cause>.pdf left behind (2026-09-24 review) -----


def test_a_leftover_plain_file_becomes_a_sibling(inbox):
    """The old agent (or a day when the cause had one item) left the Order of
    Commitment as <cause>.pdf. Under that name the watcher could let it replace
    the real PC; it must end up under a sibling name."""
    (inbox / f"{CAUSE}.pdf").write_bytes(b"%PDF an older county upload")
    cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=True)

    assert not (inbox / f"{CAUSE}.pdf").exists()
    names = sorted(p.name for p in inbox.glob("*.pdf"))
    assert all(re.fullmatch(rf"{CAUSE} item-([0-9a-f]{{8}}|[0-9a-f]{{64}})\.pdf", n) for n in names), names
    retired = [n for n in names if re.fullmatch(rf"{CAUSE} item-[0-9a-f]{{64}}\.pdf", n)]
    assert len(retired) == 1, "the leftover takes a full-hash name, never an item's 8-character one"
    contents = sorted(p.read_bytes() for p in inbox.glob("*.pdf"))
    assert contents == sorted(list(CONTENTS.values()) + [b"%PDF an older county upload"])


def test_a_leftover_that_is_one_of_the_items_is_removed(inbox):
    (inbox / f"{CAUSE}.pdf").write_bytes(CONTENTS["25-501953 OC.pdf"])
    cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=True)

    assert not (inbox / f"{CAUSE}.pdf").exists()
    assert sorted(p.read_bytes() for p in inbox.glob("*.pdf")) == sorted(CONTENTS.values())


# ----- one broken item never blocks the rest -----


class _BrokenFirstClient(_FakeClient):
    def download_dme_file(self, service_id, item):
        if item.name == "Vazquez PC.pdf":
            raise RuntimeError("proxy 500")
        return super().download_dme_file(service_id, item)


@pytest.mark.parametrize("force_pc", [True, False])
def test_refresh_keeps_going_past_a_failed_item(inbox, force_pc):
    result = cli._refresh_one_case(CAUSE, _BrokenFirstClient(GIOVANY, CONTENTS), force_pc=force_pc)

    assert "proxy 500" in (result["error"] or "")
    assert result["pcs_downloaded"] == 2
    assert sorted(p.read_bytes() for p in inbox.glob("*.pdf")) == sorted(
        [CONTENTS["C1CR25501953.pdf"], CONTENTS["25-501953 OC.pdf"]])
    seen = storage.load_seen_dme_ids()
    assert storage.dme_fingerprint(GIOVANY[0]) not in seen, "the failed item is retried next run"
    assert {storage.dme_fingerprint(i) for i in GIOVANY[1:]} <= seen


def test_refresh_marks_every_delivered_item_seen(inbox):
    cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=False)
    assert {storage.dme_fingerprint(i) for i in GIOVANY} <= storage.load_seen_dme_ids()


# ----- the other three write paths -----


def _video(name="bodycam.mp4"):
    return DMEItem(
        name=name, type="Video - BodyCam", source="Government", size="1 KB",
        available_date="9/10/2026", last_accessed_date=None, is_archived=False,
        enclosure_href="https://example/dmefile?dmeId=99999999-9999-9999-9999-999999999999",
        api_href=None,
    )


class _CaseStub:
    case_number = CAUSE
    status = "Active"
    defendant_name = "X"


class _FetchClient(_FakeClient):
    def get_case_detail(self, service_id, case_uuid):
        return _CaseStub()

    def download_dme_file_to_path(self, service_id, item, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        window = storage.expected_size_range(item)
        n = window[0] if window else 1
        path.write_bytes(b"x" * n)
        return n


@pytest.fixture
def wired(inbox, monkeypatch, tmp_path):
    items = GIOVANY + [_video()]
    client = _FetchClient(items, CONTENTS)
    monkeypatch.setattr(cli, "TechShareSession", lambda: types.SimpleNamespace(ensure_authenticated=lambda: None))
    monkeypatch.setattr(cli, "TechShareClient", lambda session: client)
    monkeypatch.setattr(cli.storage, "record_run_result", lambda **kw: None)
    staging = tmp_path / "staging"
    monkeypatch.setattr(storage, "case_discovery_target_path",
                        lambda item, cause, target_dir=None: staging / item.name)
    monkeypatch.setattr(storage, "is_present_in_portal", lambda item, cause: False)
    return inbox


def _pc_files(box):
    return sorted(p.read_bytes() for p in box.glob("*.pdf"))


def test_process_email_delivers_every_pc_type_item(wired, monkeypatch):
    body = (Path(__file__).parent / "fixtures" / "email_dme.txt").read_text()
    event_cause = cli.parse_email_body(body).cause_number
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))

    rc = cli.cmd_process_email(argparse.Namespace(subject=None))

    assert rc == 0
    assert _pc_files(wired) == sorted(CONTENTS.values())
    assert all(p.name.startswith(f"{event_cause} item-") for p in wired.glob("*.pdf"))
    assert {storage.dme_fingerprint(i) for i in GIOVANY} <= storage.load_seen_dme_ids()


def test_fetch_delivers_every_pc_type_item(wired):
    rc = cli.cmd_fetch(argparse.Namespace(
        cause_number=CAUSE, service_id="s", case_uuid="u", item_ids=None, target_dir=None, manifest=None))

    assert rc == 0
    assert _pc_files(wired) == sorted(CONTENTS.values()), "cmd_fetch runs smallest first, so the OC used to win"


def test_fetch_items_delivers_every_requested_pc_type_item(wired):
    ids = [storage.dme_fingerprint(i) for i in GIOVANY]
    rc = cli.cmd_fetch_items(argparse.Namespace(
        cause_number=CAUSE, service_id="s", case_uuid="u", item_ids=ids, target_dir=None, manifest=None))

    assert rc == 0
    assert _pc_files(wired) == sorted(CONTENTS.values())


def test_a_lone_pc_among_other_evidence_keeps_the_plain_name(inbox, monkeypatch, tmp_path):
    client = _FetchClient([GIOVANY[0], _video()], CONTENTS)
    monkeypatch.setattr(cli, "TechShareSession", lambda: types.SimpleNamespace(ensure_authenticated=lambda: None))
    monkeypatch.setattr(cli, "TechShareClient", lambda session: client)
    monkeypatch.setattr(storage, "case_discovery_target_path",
                        lambda item, cause, target_dir=None: tmp_path / "staging" / item.name)
    monkeypatch.setattr(storage, "is_present_in_portal", lambda item, cause: False)

    cli.cmd_fetch(argparse.Namespace(
        cause_number=CAUSE, service_id="s", case_uuid="u", item_ids=None, target_dir=None, manifest=None))

    assert [p.name for p in inbox.glob("*.pdf")] == [f"{CAUSE}.pdf"], "only PC-type items count as siblings"


# ----- second review (2026-09-24) -----


def _item_names_only(box):
    names = sorted(p.name for p in box.glob("*.pdf"))
    assert all(re.fullmatch(rf"{CAUSE} item-[0-9a-f]{{8}}\.pdf", n) for n in names), names
    return names


def test_multi_pc_paths_never_use_the_plain_name(inbox):
    cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=True)
    assert len(_item_names_only(inbox)) == 3


def test_a_failed_write_never_removes_the_leftover(inbox, monkeypatch):
    """The plain leftover holds this item's bytes; if the new copy cannot be
    written (disk full), the leftover must still be there."""
    (inbox / f"{CAUSE}.pdf").write_bytes(CONTENTS["Vazquez PC.pdf"])

    def disk_full(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "atomic_write_bytes", disk_full)
    with pytest.raises(OSError):
        storage.write_pc_affidavit(GIOVANY[0], CONTENTS["Vazquez PC.pdf"], CAUSE, force=True, pc_items=GIOVANY)
    assert (inbox / f"{CAUSE}.pdf").read_bytes() == CONTENTS["Vazquez PC.pdf"]


@pytest.mark.parametrize("typed", ["C-1-CR-25-501953", "C1CR 25501953", "c1cr25501953", "C\u20111CR25501953"])
def test_a_hand_typed_cause_gets_the_canonical_names_and_retires_the_leftover(inbox, typed):
    (inbox / f"{CAUSE}.pdf").write_bytes(b"%PDF an older county upload")
    out = storage.write_pc_affidavit(GIOVANY[0], CONTENTS["Vazquez PC.pdf"], typed, pc_items=GIOVANY)
    assert out.name.startswith(f"{CAUSE} item-")
    assert not (inbox / f"{CAUSE}.pdf").exists(), "the canonical plain leftover is retired"
    assert all(p.name.startswith(f"{CAUSE} item-") for p in inbox.glob("*.pdf"))


def _fail_write_for(monkeypatch, doomed: bytes):
    real = storage.atomic_write_bytes

    def write(path, data):
        if data == doomed:
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(storage, "atomic_write_bytes", write)
    return real


@pytest.mark.parametrize("force_pc", [True, False])
def test_refresh_write_failure_is_not_seen_and_does_not_stop_the_rest(inbox, monkeypatch, force_pc):
    _fail_write_for(monkeypatch, CONTENTS["Vazquez PC.pdf"])
    result = cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=force_pc)

    assert "PC write failed" in (result["error"] or "")
    assert result["pcs_downloaded"] == 2
    assert sorted(p.read_bytes() for p in inbox.glob("*.pdf")) == sorted(
        [CONTENTS["C1CR25501953.pdf"], CONTENTS["25-501953 OC.pdf"]])
    assert storage.dme_fingerprint(GIOVANY[0]) not in storage.load_seen_dme_ids()


def test_process_email_write_failure_is_retried_not_green_flagged(wired, monkeypatch):
    body = (Path(__file__).parent / "fixtures" / "email_dme.txt").read_text()
    real = _fail_write_for(monkeypatch, CONTENTS["Vazquez PC.pdf"])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))
    assert cli.cmd_process_email(argparse.Namespace(subject=None)) == 1
    assert storage.dme_fingerprint(GIOVANY[0]) not in storage.load_seen_dme_ids()

    monkeypatch.setattr(storage, "atomic_write_bytes", real)       # the disk has room again
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))
    assert cli.cmd_process_email(argparse.Namespace(subject=None)) == 0, "the retry delivers it"
    assert _pc_files(wired) == sorted(CONTENTS.values())


def test_an_unforced_run_also_retires_the_leftover(inbox):
    (inbox / f"{CAUSE}.pdf").write_bytes(b"%PDF an older county upload")
    cli._refresh_one_case(CAUSE, _FakeClient(GIOVANY, CONTENTS), force_pc=False)
    assert not (inbox / f"{CAUSE}.pdf").exists()


def test_a_lone_pc_is_delivered_even_when_an_older_file_holds_its_name(inbox):
    """The listing once held only the misfiled Order (delivered as <cause>.pdf);
    now it holds only the real PC. The PC must arrive and the Order be kept."""
    (inbox / f"{CAUSE}.pdf").write_bytes(CONTENTS["25-501953 OC.pdf"])
    result = cli._refresh_one_case(CAUSE, _FakeClient([GIOVANY[0]], CONTENTS), force_pc=False)

    assert result["error"] is None
    assert (inbox / f"{CAUSE}.pdf").read_bytes() == CONTENTS["Vazquez PC.pdf"]
    assert sorted(p.read_bytes() for p in inbox.glob("*.pdf")) == sorted(
        [CONTENTS["Vazquez PC.pdf"], CONTENTS["25-501953 OC.pdf"]])


def test_fetch_items_names_by_the_full_listing_not_the_request(wired):
    """Asking for just one PC-type item of several must still give it its own
    item name, or a later fetch of the others would collide with it."""
    rc = cli.cmd_fetch_items(argparse.Namespace(
        cause_number=CAUSE, service_id="s", case_uuid="u",
        item_ids=[storage.dme_fingerprint(GIOVANY[0])], target_dir=None, manifest=None))
    assert rc == 0
    assert [p.name for p in wired.glob("*.pdf")] == [storage.pc_inbox_filename(GIOVANY[0], CAUSE, GIOVANY)]


def test_process_email_keeps_going_past_a_failed_item_and_fails_the_email(wired, monkeypatch):
    body = (Path(__file__).parent / "fixtures" / "email_dme.txt").read_text()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(body))
    broken = _BrokenFirstClient(GIOVANY + [_video()], CONTENTS)
    monkeypatch.setattr(cli, "TechShareClient", lambda session: broken)
    broken.get_case_detail = lambda service_id, case_uuid: _CaseStub()

    rc = cli.cmd_process_email(argparse.Namespace(subject=None))

    assert rc == 1, "the email is retried, so the failed item is fetched again"
    assert _pc_files(wired) == sorted([CONTENTS["C1CR25501953.pdf"], CONTENTS["25-501953 OC.pdf"]])
    seen = storage.load_seen_dme_ids()
    assert storage.dme_fingerprint(GIOVANY[0]) not in seen
    assert {storage.dme_fingerprint(i) for i in GIOVANY[1:]} <= seen, "delivered items are saved even though one failed"
