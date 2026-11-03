from src.lead_utils import is_hard_break, compute_lead_tier


def test_no_https_is_hard_break():
    assert is_hard_break(["wordpress", "has_gtm", "no_https"]) is True


def test_modern_wp_gtm_is_not_hard_break():
    assert is_hard_break(
        ["wordpress", "has_gtm", "missing_email", "phone_mismatch", "render_blocking_19"]
    ) is False


def test_copyright_2024_is_not_hard_break():
    assert is_hard_break(["copyright_2024", "wordpress", "wp_outdated_3.7.1"]) is False


def test_copyright_2018_is_hard_break():
    assert is_hard_break(["copyright_2018"]) is True


def test_parked_is_hot():
    assert compute_lead_tier(85, ["parked_domain", "no_https"]) == "hot"


def test_under_construction_is_hot():
    assert compute_lead_tier(75, ["under_construction"]) == "hot"


def test_dns_failed_is_warm_or_hot():
    assert compute_lead_tier(95, ["dns_failed"]) == "hot"


def test_gtm_alone_is_cool():
    assert compute_lead_tier(45, ["has_gtm"]) == "cool"


def test_high_score_without_hard_break_is_warm():
    assert compute_lead_tier(85, ["wordpress", "has_gtm"]) == "warm"
