import re
PARKED_KEYWORDS=["domain for sale","parked","godaddy","namecheap","bluehost","hostgator"]
BUILDERS=["wix","weebly","squarespace","site123","powered by"]
def evaluate_site(url,response):
    score=0;reasons=[]
    if response is None:return 5,["unreachable"]
    if response.status_code!=200:score+=5;reasons.append("http_error")
    text=response.text.lower()
    if any(k in text for k in PARKED_KEYWORDS):score+=5;reasons.append("parked_domain")
    if url.startswith("http://"):score+=2;reasons.append("no_ssl")
    if '<meta name="viewport"' not in text:score+=1;reasons.append("not_mobile_friendly")
    if any(b in text for b in BUILDERS):score+=1;reasons.append("diy_builder")
    years=re.findall(r"(19|20)\d{2}",text)
    if years and max(map(int,years))<2019:score+=2;reasons.append("outdated_content")
    return score,reasons
