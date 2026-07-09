"""PC-affidavit detection — tolerant type matching (2026-07-09).

Patrick's review question during the first live backfill: "is it reading
the Type column, so it does not miss a PC affidavit?" Answer: yes — the
match is on TechShare's structured `type` field. These tests pin the
tolerant normalization so label drift (spacing / case / trailing
whitespace) can never silently drop a PC, while unrelated types stay
excluded.
"""

from voxhora_techshare_agent.models import DMEItem


def _item(type_str: str) -> DMEItem:
    return DMEItem(
        name="26-000000 PC.pdf",
        type=type_str,
        source="Government",
        size="768 KB",
        available_date="1/13/2026",
        last_accessed_date=None,
        is_archived=False,
        enclosure_href="https://example/dmefile?dmeId=test-id",
        api_href=None,
    )


def test_canonical_label_matches():
    assert _item("PC Affidavit / Arrest Warrant").is_pc_affidavit


def test_spacing_variants_match():
    assert _item("PC Affidavit/Arrest Warrant").is_pc_affidavit
    assert _item("PC  Affidavit / Arrest Warrant").is_pc_affidavit
    assert _item(" PC Affidavit / Arrest Warrant ").is_pc_affidavit


def test_case_variants_match():
    assert _item("pc affidavit / arrest warrant").is_pc_affidavit
    assert _item("PC AFFIDAVIT / ARREST WARRANT").is_pc_affidavit


def test_bare_pc_affidavit_matches():
    assert _item("PC Affidavit").is_pc_affidavit


def test_unrelated_types_do_not_match():
    for t in [
        "Offense Report",
        "Driving Record",
        "Video - In Car",
        "Audio - 911 Call",
        "Photographs",
        "Disclosure Notice",
        "DWI Paperwork",
        # Other affidavit kinds must NOT be treated as the PC.
        "Affidavit of Non-Prosecution",
    ]:
        assert not _item(t).is_pc_affidavit, t
