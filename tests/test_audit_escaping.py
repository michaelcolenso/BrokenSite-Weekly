"""Regression tests: audit pages must HTML-escape untrusted lead data.

Business names, cities, and categories come from scraped Google Maps data and
are rendered into the audit page served to third parties. Jinja autoescaping
must neutralize any embedded markup.
"""

from src.audit_generator import generate_audit_html


def test_business_name_is_escaped_in_audit_html():
    lead = {
        "place_id": "p1",
        "name": "<script>alert('xss')</script>Joe's Plumbing",
        "website": "https://joes.example",
        "city": "Austin, TX",
        "category": "plumber",
        "score": 80,
        "reasons": ["no_https"],
    }

    html = generate_audit_html(lead, tracking_base_url="https://track.example")

    assert html is not None
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;" in html


def test_website_field_is_escaped_in_audit_html():
    lead = {
        "place_id": "p2",
        "name": "Acme Co",
        "website": '"><img src=x onerror=alert(1)>',
        "city": "Denver, CO",
        "category": "roofer",
        "score": 70,
        "reasons": ["no_https"],
    }

    html = generate_audit_html(lead, tracking_base_url="https://track.example")

    assert html is not None
    assert "<img src=x onerror=alert(1)>" not in html
