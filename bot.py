import csv,sqlite3,requests
from config import *
from maps_scraper import scrape_businesses
from scoring import evaluate_site
def init_db():
    conn=sqlite3.connect(STATE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS leads (website TEXT PRIMARY KEY)")
    conn.commit();return conn
def seen(conn,website):
    return conn.execute("SELECT 1 FROM leads WHERE website=?",(website,)).fetchone() is not None
def mark_seen(conn,website):
    conn.execute("INSERT OR IGNORE INTO leads VALUES (?)",(website,));conn.commit()
def main():
    conn=init_db();exported=[]
    for city in CITIES:
        for category in CATEGORIES:
            businesses=scrape_businesses(city,category,MAX_RESULTS_PER_QUERY)
            for biz in businesses:
                if seen(conn,biz["website"]):continue
                try:resp=requests.get(biz["website"],timeout=REQUEST_TIMEOUT,allow_redirects=True)
                except:resp=None
                score,reasons=evaluate_site(biz["website"],resp)
                if score>=MIN_SCORE_TO_EXPORT:
                    exported.append({**biz,"score":score,"reasons":",".join(reasons)})
                mark_seen(conn,biz["website"])
    with open(OUTPUT_FILE,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["name","website","city","category","score","reasons"])
        w.writeheader();w.writerows(exported)
if __name__=="__main__":main()
