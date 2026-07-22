from scanner.run import choose_internal_urls, current_iso_week


def test_current_iso_week_formats_week():
    from datetime import date

    assert current_iso_week(date(2026, 7, 22)) == "2026-W30"


def test_choose_internal_urls_prefers_contact_then_one_internal():
    html = '''
    <a href="/about">About</a>
    <a href="https://elsewhere.test/contact">External</a>
    <a href="/contact-us">Contact</a>
    <a href="/services">Services</a>
    '''

    assert choose_internal_urls("https://example.com/", html) == [
        "https://example.com/contact-us",
        "https://example.com/about",
    ]
