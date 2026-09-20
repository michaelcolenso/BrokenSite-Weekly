"""Tests for scripts/build_metro_csv.py (unofficial export -> v1 metro CSV)."""

import csv
import json

from scripts.build_metro_csv import (
    load_blocklist,
    load_records,
    main,
    normalize_records,
    write_metro_csv,
)


def write_blocklist(tmp_path, domains=("mcdonalds.com",)):
    path = tmp_path / "blocklist.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["domain", "reason"])
        for domain in domains:
            writer.writerow([domain, "national chain"])
    return path


def stats_fresh():
    return {}


# --- normalize_records ---


def test_normalize_maps_alias_fields_and_strips_www(tmp_path):
    records = [{
        "title": "Acme Plumbing",
        "category": "Plumber",
        "phone": "206-555-0100",
        "full_address": "1 Pike St, Seattle, WA",
        "site": "https://www.acmeplumbing.com/",
    }]
    rows = normalize_records(records, blocklist=set(), stats=stats_fresh())
    assert rows == [{
        "business_name": "Acme Plumbing",
        "vertical": "Plumber",
        "phone": "206-555-0100",
        "address": "1 Pike St, Seattle, WA",
        "domain": "acmeplumbing.com",
    }]


def test_normalize_drops_rows_without_website(tmp_path):
    stats = stats_fresh()
    rows = normalize_records([{"name": "No Site LLC", "phone": "206-555-0101"}], blocklist=set(), stats=stats)
    assert rows == []
    assert stats["no_website"] == 1


def test_normalize_drops_social_only_destinations():
    stats = stats_fresh()
    records = [
        {"name": "FB Only", "site": "https://facebook.com/fbonly"},
        {"name": "Linktree Co", "site": "https://linktr.ee/linktreeco"},
        {"name": "Real Site", "site": "https://realsite.com"},
    ]
    rows = normalize_records(records, blocklist=set(), stats=stats)
    assert [row["business_name"] for row in rows] == ["Real Site"]
    assert stats["social_only"] == 2


def test_normalize_drops_blocklisted_chains_including_subdomains(tmp_path):
    blocklist = load_blocklist(write_blocklist(tmp_path))
    stats = stats_fresh()
    records = [
        {"name": "McDonalds", "site": "https://www.mcdonalds.com"},
        {"name": "McDonalds Local", "site": "https://locations.mcdonalds.com/wa/seattle"},
        {"name": "Indie Burger", "site": "https://indieburger.com"},
    ]
    rows = normalize_records(records, blocklist=blocklist, stats=stats)
    assert [row["business_name"] for row in rows] == ["Indie Burger"]
    assert stats["chain"] == 2


def test_normalize_dedupes_by_domain_then_identity():
    stats = stats_fresh()
    records = [
        {"name": "Acme", "address": "1 Pike St", "site": "https://acme.com"},
        {"name": "Acme Duplicate", "site": "https://acme.com"},
        {"name": "Acme", "address": "1 Pike St", "site": "https://acme-plumbing.com"},
    ]
    rows = normalize_records(records, blocklist=set(), stats=stats)
    assert len(rows) == 1
    assert stats["dupe_domain"] == 1
    assert stats["dupe_identity"] == 1


# --- load_records ---


def test_load_records_json_array_and_wrapped(tmp_path):
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps([{"name": "A"}]))
    assert load_records(plain) == [{"name": "A"}]

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"query": "plumber seattle", "results": [{"name": "B"}]}))
    assert load_records(wrapped) == [{"name": "B"}]


def test_load_records_csv(tmp_path):
    path = tmp_path / "dump.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["business_name", "website_link"])
        writer.writerow(["Acme", "https://acme.com"])
    assert load_records(path) == [{"business_name": "Acme", "website_link": "https://acme.com"}]


# --- write_metro_csv / append ---


def test_write_metro_csv_append_dedupes_against_existing(tmp_path):
    path = tmp_path / "metros" / "seattle.csv"
    write_metro_csv([{"business_name": "A", "vertical": "", "phone": "", "address": "", "domain": "a.com"}], path)
    written = write_metro_csv([
        {"business_name": "A2", "vertical": "", "phone": "", "address": "", "domain": "a.com"},
        {"business_name": "B", "vertical": "", "phone": "", "address": "", "domain": "b.com"},
    ], path, append=True)
    assert written == 1
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["domain"] for row in rows] == ["a.com", "b.com"]


# --- end-to-end via main() ---


def test_main_end_to_end(tmp_path):
    blocklist = write_blocklist(tmp_path)
    raw = tmp_path / "export.json"
    raw.write_text(json.dumps([
        {"title": "Acme Plumbing", "category": "Plumber", "phone": "206-555-0100",
         "full_address": "1 Pike St", "site": "https://acmeplumbing.com"},
        {"title": "No Website Co", "category": "Roofer"},
        {"title": "FB Only", "site": "https://facebook.com/fbonly"},
        {"title": "McDonalds", "site": "https://mcdonalds.com"},
    ]))
    exit_code = main([
        "--input", str(raw),
        "--metro", "seattle",
        "--data-dir", str(tmp_path / "metros"),
        "--blocklist", str(blocklist),
    ])
    assert exit_code == 0
    with (tmp_path / "metros" / "seattle.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["domain"] == "acmeplumbing.com"
    assert rows[0]["vertical"] == "Plumber"
