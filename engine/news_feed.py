#!/usr/bin/env python3
"""Auto-refresh catalyst.json from keyless RSS (cointelegraph, CNBC, Yahoo Finance).
Skips if catalyst.json is fresher than MAX_AGE_MIN. Called by alpaca_cycle.sh each tick.
Design: titles-only news — Jev consumes them as facts; no LLM in this path."""
import json, os, re, sys, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.path.join(HERE, "catalyst.json")
MAX_AGE_MIN = 30
FEEDS = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "yahoo": "https://finance.yahoo.com/news/rssindex",
}
KEYWORDS = ("bitcoin", "btc", "ethereum", "eth ", "crypto", "fed", "rate", "inflation",
            "cpi", "ppi", "nasdaq", "s&p", "stock", "market", "treasury", "yield",
            "oil", "sec ", "cftc", "etf", "hike", "cut", "solana", "ripple", "xrp")

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")

def titles_from(xml_text, cap=6):
    out = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            t = (item.findtext("title") or "").strip()
            if t:
                out.append(re.sub(r"\s+", " ", t))
            if len(out) >= cap:
                break
    except ET.ParseError:
        out = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text)[1:1 + cap]
    return out

def main():
    try:
        if os.path.exists(CAT):
            age = (time.time() - os.path.getmtime(CAT)) / 60
            if age < MAX_AGE_MIN:
                return  # fresh enough, stay quiet
    except OSError:
        pass
    news = []
    for name, url in FEEDS.items():
        try:
            for t in titles_from(fetch(url)):
                low = " " + t.lower() + " "
                if any(k in low for k in KEYWORDS):
                    news.append(f"[{name}] {t[:140]}")
        except Exception as e:
            print(f"feed {name} failed: {str(e)[:80]}", file=sys.stderr)
    if len(news) < 3:
        print("too few news items, keeping old catalysts", file=sys.stderr)
        return
    # merge with prior regime-read lines (anything we curated without [source] prefix)
    prior = []
    try:
        prior = [c for c in json.load(open(CAT)).get("catalysts", []) if not c.startswith("[")]
    except Exception:
        pass
    cats = (news[:8] + prior[-2:])
    tmp = CAT + ".tmp"
    json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
               "catalysts": cats}, open(tmp, "w"), indent=1)
    os.replace(tmp, CAT)
    print(f"catalyst refreshed: {len(news)} live headlines")

if __name__ == "__main__":
    main()
