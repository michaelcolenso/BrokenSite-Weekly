from playwright.sync_api import sync_playwright
import time
def scrape_businesses(city,category,limit):
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        page.goto(f"https://www.google.com/maps/search/{category}+in+{city}")
        page.wait_for_timeout(5000)
        for _ in range(6):
            page.mouse.wheel(0,4000);page.wait_for_timeout(2000)
        cards=page.query_selector_all("div[role='article']")
        for card in cards[:limit]:
            try:
                card.click();page.wait_for_timeout(3000)
                name_el=page.query_selector("h1")
                site_el=page.query_selector("a[data-item-id='authority']")
                if not name_el or not site_el:continue
                results.append({"name":name_el.inner_text(),"website":site_el.get_attribute("href"),"city":city,"category":category})
            except:continue
        browser.close()
    return results
