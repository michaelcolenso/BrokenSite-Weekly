from pathlib import Path

from src.blocklist import is_blocked, is_junk_website, load_blocklist


def test_rotorooter_is_blocked():
    blocked = load_blocklist()
    assert is_blocked("https://www.rotorooter.com/seattle/", blocked) is True


def test_local_plumber_not_blocked():
    blocked = load_blocklist()
    assert is_blocked("https://bens.plumbing/", blocked) is False


def test_gbp_create_is_junk():
    url = (
        "https://business.google.com/create?fp=1&hl=fi&authuser=0"
    )
    assert is_junk_website(url) is True


def test_normal_site_not_junk():
    assert is_junk_website("https://example.com/") is False
