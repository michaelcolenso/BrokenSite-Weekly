from pathlib import Path

import pytest

from scanner.business_list import load_businesses, normalize_domain


def test_normalize_domain_strips_scheme_path_and_www():
    assert normalize_domain("https://www.Example.com/path") == "example.com"


def test_load_businesses_dedupes_and_applies_blocklist(tmp_path):
    csv_path = tmp_path / "metro.csv"
    csv_path.write_text(
        "business_name,vertical,phone,address,domain\n"
        "A,plumber,1,Addr,https://www.local.com/path\n"
        "Duplicate,plumber,2,Addr,local.com\n"
        "Chain,coffee,3,Addr,starbucks.com\n",
        encoding="utf-8",
    )
    blocklist = tmp_path / "blocklist.csv"
    blocklist.write_text("domain,reason\nstarbucks.com,national chain\n", encoding="utf-8")

    businesses = load_businesses(csv_path, blocklist_path=blocklist)

    assert len(businesses) == 1
    assert businesses[0].domain == "local.com"
    assert businesses[0].business_name == "A"


def test_load_businesses_requires_expected_columns(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,domain\nA,example.com\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_businesses(csv_path)
