#!/usr/bin/env python3
"""
Deal Finder — watches the web for deals (incl. bulk lots) on products you want to resell.

Sources
  * Slickdeals  (RSS keyword search, no key needed)        – electronics, toys
  * Reddit      (RSS search in subreddits you choose)       – r/buildapcsales, r/gamesale, r/legodeal ...
  * eBay        (official Browse API, free developer keys)  – lots, bundles, retro games

Standard library only: no pip install needed. Python 3.9+.

Run:   python3 dealfinder.py            then open http://localhost:8765
       (on your phone: http://<this-computer's-LAN-IP>:8765)
Other: python3 dealfinder.py --scan-once   (one scan, print results, exit)
       python3 dealfinder.py --port 9000
"""
import argparse
import base64
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DEALFINDER_DB", os.path.join(HERE, "deals.db"))
USER_AGENT = "DealFinder/1.0 (personal deal watcher)"
CATEGORIES = ["Electronics", "Retro games", "Toys"]
SOURCES = ["slickdeals", "reddit", "ebay"]

DEFAULT_SETTINGS = {
    "scan_minutes": 30,            # 0 = auto-scan off (scan only when you tap "Scan now")
    "show_images": True,           # turn off to save mobile data
    "fee_percent": 13.25,          # marketplace fee you'll pay when reselling (eBay ~13%)
    "min_profit_alert": 20,        # push alert when estimated profit >= this ($)
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",              # pick something unguessable, e.g. dealfinder-7f3k29
    "ebay_client_id": "",
    "ebay_client_secret": "",
    "serpapi_key": "",             # optional free key from serpapi.com: Amazon/Walmart/Target/Home Depot + Facebook Marketplace
    "location": "",                # "City, State" for Facebook Marketplace searches
    "subreddits": {
        "Electronics": "buildapcsales",
        "Retro games": "gamesale",
        "Toys": "legodeal",
    },
}

STARTER_WATCHES = [
    # name, category, keywords, exclude, max_price, min_qty
    ("AirPods Pro", "Electronics", "airpods pro", "case only, replacement", 160, 1),
    ("2TB NVMe SSD", "Electronics", "2tb nvme", "", 120, 1),
    ("Game Boy game lots", "Retro games", "game boy lot", "repro, reproduction, for parts, broken", 150, 3),
    ("N64 game lots", "Retro games", "n64 lot", "repro, reproduction, for parts, broken", 200, 3),
    ("LEGO sets", "Toys", "lego", "minifig only, instructions only, knock off", 80, 1),
    ("Hot Wheels lots", "Toys", "hot wheels lot", "", 60, 10),
]

STATE = {"scanning": False, "last_scan": None, "next_scan": time.time() + 5, "last_error": None,
         "manual": False, "bytes": 0, "last_bytes": None, "last_new": None}
SCAN_LOCK = threading.Lock()
_EBAY_TOKEN = {"token": None, "exp": 0}


# --------------------------------------------------------------------------- database
class db:
    """Connection context manager: commits on success, rolls back on error, always closes."""
    def __enter__(self):
        self.con = sqlite3.connect(DB_PATH, timeout=15)
        self.con.row_factory = sqlite3.Row
        return self.con

    def __exit__(self, exc_type, *a):
        try:
            self.con.rollback() if exc_type else self.con.commit()
        finally:
            self.con.close()


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, category TEXT NOT NULL, keywords TEXT NOT NULL,
                exclude TEXT DEFAULT '', max_price REAL, resale_value REAL,
                min_qty INTEGER DEFAULT 1, sources TEXT DEFAULT '["slickdeals","reddit","ebay"]',
                active INTEGER DEFAULT 1, created TEXT);
            CREATE TABLE IF NOT EXISTS deal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL, source TEXT, title TEXT, url TEXT,
                price REAL, shipping REAL, qty INTEGER DEFAULT 1, bulk INTEGER DEFAULT 0,
                image TEXT, posted TEXT, found_at TEXT,
                profit REAL, margin REAL,
                starred INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0, alerted INTEGER DEFAULT 0,
                UNIQUE(watch_id, url));
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, watch TEXT,
                status TEXT, found INTEGER, message TEXT);
            """
        )
        if con.execute("SELECT COUNT(*) FROM watch").fetchone()[0] == 0 and \
                con.execute("SELECT value FROM settings WHERE key='seeded'").fetchone() is None:
            for name, cat, kw, ex, mx, mq in STARTER_WATCHES:
                con.execute(
                    "INSERT INTO watch(name,category,keywords,exclude,max_price,min_qty,created) VALUES (?,?,?,?,?,?,?)",
                    (name, cat, kw, ex, mx, mq, now_iso()))
            con.execute("INSERT OR REPLACE INTO settings VALUES ('seeded','1')")


def get_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    if row:
        saved = json.loads(row["value"])
        for k, v in saved.items():
            if k == "subreddits" and isinstance(v, dict):
                s["subreddits"].update(v)
            elif k in s:
                s[k] = v
    return s


def save_settings(new):
    s = get_settings()
    for k in DEFAULT_SETTINGS:
        if k not in new:
            continue
        v = new[k]
        if k == "ebay_client_secret" and (v or "").startswith("••"):
            continue  # masked placeholder, keep existing
        if k in ("scan_minutes", "fee_percent", "min_profit_alert"):
            v = float(v)
            if k == "scan_minutes":
                v = 0.0 if v <= 0 else max(5.0, v)  # 0 = off; otherwise be polite to the sites
        if k == "show_images":
            v = v in (True, 1, "1", "true", "on")
        s[k] = v
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES ('config', ?)", (json.dumps(s),))
    schedule_next()
    _EBAY_TOKEN.update(token=None, exp=0)
    return s


def log_scan(source, watch, status, found, message="", con=None):
    if con is None:
        with db() as c:
            return log_scan(source, watch, status, found, message, c)
    if True:
        con.execute("INSERT INTO scan_log(ts,source,watch,status,found,message) VALUES (?,?,?,?,?,?)",
                    (now_iso(), source, watch, status, found, message[:300]))
        con.execute("DELETE FROM scan_log WHERE id NOT IN (SELECT id FROM scan_log ORDER BY id DESC LIMIT 300)")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- parsing helpers
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?")
QTY_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\blot of (\d{1,3})\b",
        r"\bcase of (\d{1,3})\b",
        r"\bbundle of (\d{1,3})\b",
        r"\bset of (\d{1,3})\b",
        r"\b(\d{1,3})[- ]?(?:pack|pk|count|ct)\b",
        r"\b(\d{1,3}) ?x\b(?! ?\d)",
        r"\bx ?(\d{1,3})\b",
        r"\b(\d{1,3})\s+(?:games|cartridges|carts|cart|units|pcs|pieces|figures|cars|sets)\b",
        r"\b(\d{1,3})[- ]game lot\b",
    )
]
BULK_WORDS = re.compile(r"\b(lot|lots|bundle|bulk|wholesale|case|pallet|multi-?pack)\b", re.I)
IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def parse_price(text):
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "") + (m.group(2) or ""))
    except ValueError:
        return None


def parse_qty(text):
    for p in QTY_PATTERNS:
        m = p.search(text or "")
        if m:
            q = int(m.group(1))
            if 1 < q <= 1000:
                return q
    return 1


def is_bulk(title, qty):
    return qty > 1 or bool(BULK_WORDS.search(title or ""))


def split_words(s):
    return [w.strip().lower() for w in (s or "").split(",") if w.strip()]


def title_matches(title, keywords, exclude):
    t = (title or "").lower()
    if any(x in t for x in split_words(exclude)):
        return False
    tokens = [k for k in re.split(r"\s+", (keywords or "").lower()) if k]
    return all(tok in t for tok in tokens)


def compute_profit(price, shipping, qty, resale, fee_percent):
    if price is None or not resale:
        return None, None
    cost = float(price) + float(shipping or 0)
    revenue = float(resale) * max(1, int(qty or 1)) * (1 - float(fee_percent) / 100.0)
    profit = round(revenue - cost, 2)
    margin = round(profit / cost * 100, 1) if cost > 0 else None
    return profit, margin


def to_iso(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# --------------------------------------------------------------------------- network
def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        STATE["bytes"] += len(data) + 600  # + rough allowance for headers
        return data


def http_post(url, data, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        STATE["bytes"] += len(data) + 600  # + rough allowance for headers
        return data


# --------------------------------------------------------------------------- sources
STORES = ["Amazon", "Walmart", "Target", "Home Depot", "Best Buy", "Costco", "Sam's Club", "Lowe's", "Kohl's", "GameStop",
          "Newegg", "Woot", "eBay", "Macy's", "LEGO", "Dell", "Staples", "Office Depot", "Microsoft", "Nintendo", "Meijer",
          "Big Lots", "Five Below", "Ollie's", "B&H", "Adorama", "Lenovo", "HP", "Samsung", "Apple", "Kroger", "Dick's",
          "Academy", "Wayfair", "Ulta", "Sephora", "JCPenney", "Belk", "Barnes & Noble", "AliExpress", "Temu", "Wish"]
BRANDS = {"LEGO", "Nintendo", "Microsoft", "Samsung", "Apple", "Dell", "HP", "Lenovo"}  # also brands: only count after "at"/"from"
def _store_rx(st):
    return re.escape(st).replace("'", "['’]?").replace("\\ ", r"\s+") + r"(?:\.com)?"
_ANY = "|".join(_store_rx(st) for st in sorted(STORES, key=len, reverse=True))
_AT_RE = re.compile(r"(?:\bat|\bfrom|\bvia|@|[-–|(])\s*(?:the\s+)?(" + _ANY + r")\b", re.I)
_STORE_RE = [(st, re.compile(r"\b" + _store_rx(st) + r"\b", re.I)) for st in STORES]
_HAS_RE = re.compile(r"\b(" + _ANY + r")\s+(?:has|is offering|offers|is having|now has|is selling|currently has)\b", re.I)


def _canon(name):
    n = re.sub(r"\.com$", "", name.strip(), flags=re.I).replace("’", "'").lower()
    for st in STORES:
        if re.sub(r"['\s]", "", st.lower()) == re.sub(r"['\s]", "", n):
            return st
    return None


def detect_store(title, desc=""):
    """Find the retailer: '... at Walmart' style in the title first, then the description's opening, then any non-brand store name."""
    title = html.unescape(title or "")
    desc = re.sub(r"<[^>]+>", " ", html.unescape(desc or ""))[:400]
    for t in (title, desc):
        for rx in (_AT_RE, _HAS_RE):
            m = rx.search(t)
            if m and _canon(m.group(1)):
                return _canon(m.group(1))
    for t in (desc, title):
        best = None
        for st, rx in _STORE_RE:
            if st in BRANDS:
                continue
            m = rx.search(t)
            if m and (best is None or m.start() < best[1]):
                best = (st, m.start())
        if best:
            return best[0]
    return None


def src_slickdeals(watch, settings):
    q = urllib.parse.quote_plus(watch["keywords"])
    url = f"https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1"
    root = ET.fromstring(http_get(url))
    out = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        desc = item.findtext("description") or ""
        content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        img = IMG_RE.search(content + desc)
        out.append({
            "title": title,
            "store": detect_store(title, desc + " " + content),
            "url": (item.findtext("link") or "").strip(),
            "price": parse_price(title),
            "shipping": None,
            "image": img.group(1) if img else None,
            "posted": to_iso(item.findtext("pubDate")),
        })
    return out


ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def src_reddit(watch, settings):
    subs = split_words(settings["subreddits"].get(watch["category"], ""))
    out = []
    for sub in subs:
        q = urllib.parse.quote_plus(watch["keywords"])
        url = f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/search.rss?q={q}&restrict_sr=on&sort=new&t=month"
        root = ET.fromstring(http_get(url))
        for e in root.iter(ATOM + "entry"):
            title = html.unescape(e.findtext(ATOM + "title") or "").strip()
            link = e.find(ATOM + "link")
            thumb = e.find(MEDIA + "thumbnail")
            content = e.findtext(ATOM + "content") or ""
            img = thumb.get("url") if thumb is not None else None
            if not img:
                m = IMG_RE.search(html.unescape(content))
                img = m.group(1) if m else None
            out.append({
                "title": title, "suffix": f"r/{sub}",
                "url": link.get("href") if link is not None else "",
                "price": parse_price(title),
                "shipping": None,
                "image": img,
                "posted": to_iso(e.findtext(ATOM + "updated") or e.findtext(ATOM + "published")),
            })
        time.sleep(1.5)  # reddit rate limits unauthenticated clients
    return out


def ebay_token(settings):
    if _EBAY_TOKEN["token"] and time.time() < _EBAY_TOKEN["exp"] - 60:
        return _EBAY_TOKEN["token"]
    cred = base64.b64encode(f'{settings["ebay_client_id"]}:{settings["ebay_client_secret"]}'.encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    data = json.loads(http_post("https://api.ebay.com/identity/v1/oauth2/token", body, {
        "Authorization": f"Basic {cred}",
        "Content-Type": "application/x-www-form-urlencoded",
    }))
    _EBAY_TOKEN.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 7200)))
    return _EBAY_TOKEN["token"]


def src_ebay(watch, settings):
    if not (settings["ebay_client_id"] and settings["ebay_client_secret"]):
        raise SkipSource("no eBay keys set (Settings)")
    filters = ["buyingOptions:{FIXED_PRICE}", "priceCurrency:USD"]
    if watch["max_price"]:
        filters.append(f"price:[..{float(watch['max_price']):.2f}]")
    params = urllib.parse.urlencode({
        "q": watch["keywords"], "limit": 50, "sort": "newlyListed", "filter": ",".join(filters)})
    data = json.loads(http_get(
        f"https://api.ebay.com/buy/browse/v1/item_summary/search?{params}",
        {"Authorization": f"Bearer {ebay_token(settings)}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}))
    out = []
    for it in data.get("itemSummaries", []) or []:
        price = it.get("price", {}).get("value")
        ship = None
        for so in it.get("shippingOptions", []) or []:
            if so.get("shippingCost", {}).get("value") is not None:
                ship = float(so["shippingCost"]["value"])
                break
        cond = it.get("condition")
        out.append({
            "title": it.get("title", ""), "suffix": cond, "store": "eBay",
            "url": it.get("itemWebUrl", ""),
            "price": float(price) if price is not None else None,
            "shipping": ship,
            "image": (it.get("image") or {}).get("imageUrl"),
            "posted": to_iso(it.get("itemCreationDate")),
        })
    return out


class SkipSource(Exception):
    pass


SERPAPI = "https://serpapi.com/search.json"


def serpapi_get(params, settings):
    if not settings.get("serpapi_key"):
        raise SkipSource("no SerpApi key (free at serpapi.com; add SERPAPI_KEY as a repo secret)")
    q = urllib.parse.urlencode({**params, "api_key": settings["serpapi_key"]})
    try:
        data = json.loads(http_get(f"{SERPAPI}?{q}", timeout=40))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"SerpApi HTTP {e.code}: {msg or e.reason}")
    if data.get("error") and not data.get("search_metadata", {}).get("status") == "Success":
        if "hasn't returned any results" in data["error"]:
            return data
        raise RuntimeError("SerpApi: " + data["error"])
    return data


def serpapi_left(settings):
    """Searches left this month (the account endpoint doesn't use a search)."""
    if not settings.get("serpapi_key"):
        return None
    try:
        a = json.loads(http_get("https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": settings["serpapi_key"]})))
        return a.get("total_searches_left", a.get("plan_searches_left"))
    except Exception:
        return None


def src_shopping(watch, settings):
    """Google Shopping through SerpApi: one search covers Amazon, Walmart, Target, Home Depot, Best Buy and more."""
    params = {"engine": "google_shopping", "q": watch["keywords"], "gl": "us", "hl": "en"}
    data = serpapi_get(params, settings)
    out = []
    for it in data.get("shopping_results", []) or []:
        price = it.get("extracted_price")
        out.append({
            "title": it.get("title", ""),
            "store": _canon(it.get("source") or "") or (it.get("source") or "").replace(".com", "") or None,
            "url": it.get("link") or it.get("product_link") or "",
            "price": float(price) if isinstance(price, (int, float)) else parse_price(it.get("price", "")),
            "shipping": 0.0 if "free" in str(it.get("delivery", "")).lower() else None,
            "image": it.get("thumbnail"),
            "posted": None,
            "suffix": it.get("second_hand_condition"),
        })
    return out


def src_marketplace(watch, settings):
    """Facebook Marketplace through SerpApi (needs your area)."""
    if not settings.get("location"):
        raise SkipSource("set your area in Settings for Facebook Marketplace")
    data = serpapi_get({"engine": "facebook_marketplace", "q": watch["keywords"], "location": settings["location"]}, settings)
    items = data.get("organic_results") or []
    out = []
    for it in items:
        price = it.get("extracted_price")
        out.append({
            "title": it.get("title", ""),
            "store": "Facebook Marketplace",
            "url": it.get("link", ""),
            "price": float(price) if isinstance(price, (int, float)) else parse_price(str(it.get("price", ""))),
            "shipping": None,
            "image": it.get("thumbnail"),
            "posted": None,
            "suffix": it.get("location"),
        })
    return out


SOURCE_FUNCS = {"slickdeals": src_slickdeals, "reddit": src_reddit, "ebay": src_ebay,
                "shopping": src_shopping, "marketplace": src_marketplace}


# --------------------------------------------------------------------------- alerts
def notify(settings, title, message, click=None, tags="moneybag"):
    if not settings.get("ntfy_topic"):
        return False
    url = settings["ntfy_server"].rstrip("/") + "/" + urllib.parse.quote(settings["ntfy_topic"])
    headers = {"Title": title.encode("latin-1", "replace").decode("latin-1"), "Tags": tags}
    if click:
        headers["Click"] = click
    http_post(url, message.encode("utf-8"), headers)
    return True


# --------------------------------------------------------------------------- scanner
def scan_watch(con, watch, settings):
    new_deals = []
    sources = json.loads(watch["sources"] or "[]")
    had_deals = con.execute("SELECT COUNT(*) FROM deal WHERE watch_id=?", (watch["id"],)).fetchone()[0] > 0
    for src in sources:
        fn = SOURCE_FUNCS.get(src)
        if not fn:
            continue
        try:
            items = fn(watch, settings)
        except SkipSource as e:
            log_scan(src, watch["name"], "skipped", 0, str(e), con=con)
            continue
        except urllib.error.HTTPError as e:
            log_scan(src, watch["name"], "error", 0, f"HTTP {e.code}: {e.reason}", con=con)
            continue
        except Exception as e:
            log_scan(src, watch["name"], "error", 0, f"{type(e).__name__}: {e}", con=con)
            continue
        added = 0
        for it in items:
            if not it["url"] or not title_matches(it["title"], watch["keywords"], watch["exclude"]):
                continue
            if watch["max_price"] and it["price"] is not None and it["price"] > watch["max_price"]:
                continue
            qty = parse_qty(it["title"])
            if (watch["min_qty"] or 1) > 1 and qty < watch["min_qty"]:
                continue
            profit, margin = compute_profit(it["price"], it["shipping"], qty, watch["resale_value"],
                                            settings["fee_percent"])
            cur = con.execute(
                """INSERT OR IGNORE INTO deal(watch_id,source,title,url,price,shipping,qty,bulk,image,posted,
                   found_at,profit,margin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (watch["id"], src, it["title"] + (f"  · {it['suffix']}" if it.get("suffix") else ""), it["url"], it["price"], it["shipping"], qty,
                 int(is_bulk(it["title"], qty)), it["image"], it["posted"], now_iso(), profit, margin))
            if cur.rowcount:
                added += 1
                new_deals.append((cur.lastrowid, it, profit))
        log_scan(src, watch["name"], "ok", added, f"{len(items)} results, {added} new matches", con=con)
        time.sleep(1)

    # Alerts: skip a watch's very first scan so you don't get flooded with old listings
    if had_deals:
        threshold = float(settings["min_profit_alert"])
        for deal_id, it, profit in new_deals:
            hit = (profit is not None and profit >= threshold) or \
                  (profit is None and not watch["resale_value"])
            if not hit:
                continue
            price = f"${it['price']:.2f}" if it["price"] is not None else "price ?"
            msg = f"{price} — {it['title']}"
            if profit is not None:
                msg += f"\nEst. profit ${profit:.2f}"
            try:
                if notify(settings, f"Deal: {watch['name']}", msg, it["url"]):
                    con.execute("UPDATE deal SET alerted=1 WHERE id=?", (deal_id,))
            except Exception as e:
                log_scan("ntfy", watch["name"], "error", 0, str(e), con=con)
    return len(new_deals)


def run_scan(only_watch_id=None):
    if not SCAN_LOCK.acquire(blocking=False):
        return 0
    STATE["scanning"] = True
    STATE["bytes"] = 0
    total = 0
    try:
        settings = get_settings()
        with db() as con:
            q = "SELECT * FROM watch WHERE active=1" + (" AND id=?" if only_watch_id else "")
            watches = con.execute(q, (only_watch_id,) if only_watch_id else ()).fetchall()
        for w in watches:
            with db() as con:
                total += scan_watch(con, w, settings)
        STATE["last_error"] = None
    except Exception as e:
        STATE["last_error"] = str(e)
        traceback.print_exc()
    finally:
        STATE["scanning"] = False
        STATE["last_scan"] = now_iso()
        STATE["last_bytes"] = STATE["bytes"]
        STATE["last_new"] = total
        log_scan("scan", "all" if not only_watch_id else f"watch #{only_watch_id}", "ok", total,
                 f"finished: {total} new deals, ~{STATE['bytes'] / 1024:.0f} KB downloaded")
        schedule_next()
        SCAN_LOCK.release()
    return total


def auto_minutes():
    if STATE["manual"]:
        return 0
    return float(get_settings()["scan_minutes"] or 0)


def schedule_next(first=False):
    m = auto_minutes()
    STATE["next_scan"] = float("inf") if m <= 0 else time.time() + (5 if first else m * 60)


def scheduler():
    while True:
        if time.time() >= STATE["next_scan"] and not STATE["scanning"]:
            run_scan()
        time.sleep(5)


def recompute_watch(con, watch_id):
    settings = get_settings()
    w = con.execute("SELECT * FROM watch WHERE id=?", (watch_id,)).fetchone()
    if not w:
        return
    for d in con.execute("SELECT id,price,shipping,qty FROM deal WHERE watch_id=?", (watch_id,)).fetchall():
        p, m = compute_profit(d["price"], d["shipping"], d["qty"], w["resale_value"], settings["fee_percent"])
        con.execute("UPDATE deal SET profit=?, margin=? WHERE id=?", (p, m, d["id"]))


# --------------------------------------------------------------------------- web api
def num_or_none(v):
    if v in (None, ""):
        return None
    return float(v)


def api_state():
    s = get_settings()
    if s["ebay_client_secret"]:
        s["ebay_client_secret"] = "••••••••"
    with db() as con:
        watches = [dict(r) for r in con.execute(
            """SELECT w.*, (SELECT COUNT(*) FROM deal d WHERE d.watch_id=w.id AND d.dismissed=0) AS deal_count
               FROM watch w ORDER BY category, name""")]
        log = [dict(r) for r in con.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 60")]
    for w in watches:
        w["sources"] = json.loads(w["sources"] or "[]")
    return {
        "watches": watches, "settings": s, "log": log, "categories": CATEGORIES, "sources": SOURCES,
        "status": {"scanning": STATE["scanning"], "last_scan": STATE["last_scan"],
                   "next_scan_in": None if STATE["next_scan"] == float("inf") else max(0, int(STATE["next_scan"] - time.time())),
                   "auto": auto_minutes() > 0, "manual_flag": STATE["manual"],
                   "last_bytes": STATE["last_bytes"], "last_new": STATE["last_new"]},
                   "last_error": STATE["last_error"]
    }


def api_deals(qs):
    where, args = ["d.dismissed=0"], []
    if qs.get("watch"):
        where.append("d.watch_id=?"); args.append(int(qs["watch"]))
    if qs.get("category"):
        where.append("w.category=?"); args.append(qs["category"])
    if qs.get("bulk") == "1":
        where.append("d.bulk=1")
    if qs.get("starred") == "1":
        where.append("d.starred=1")
    if qs.get("q"):
        where.append("d.title LIKE ?"); args.append(f"%{qs['q']}%")
    order = {
        "profit": "d.profit IS NULL, d.profit DESC",
        "price": "d.price IS NULL, d.price ASC",
        "unit": "d.price IS NULL, (d.price*1.0/d.qty) ASC",
    }.get(qs.get("sort"), "d.found_at DESC, d.id DESC")
    sql = f"""SELECT d.*, w.name AS watch_name, w.category, w.resale_value, w.max_price
              FROM deal d JOIN watch w ON w.id=d.watch_id
              WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 300"""
    with db() as con:
        return [dict(r) for r in con.execute(sql, args)]


def api_save_watch(body):
    cat = body.get("category") if body.get("category") in CATEGORIES else CATEGORIES[0]
    srcs = [s for s in body.get("sources", SOURCES) if s in SOURCES]
    vals = (
        (body.get("name") or body.get("keywords") or "Untitled").strip(), cat,
        (body.get("keywords") or "").strip(), (body.get("exclude") or "").strip(),
        num_or_none(body.get("max_price")), num_or_none(body.get("resale_value")),
        int(body.get("min_qty") or 1), json.dumps(srcs), 1 if body.get("active", True) else 0,
    )
    if not vals[2]:
        raise ValueError("Keywords are required")
    with db() as con:
        if body.get("id"):
            con.execute("""UPDATE watch SET name=?,category=?,keywords=?,exclude=?,max_price=?,resale_value=?,
                           min_qty=?,sources=?,active=? WHERE id=?""", vals + (int(body["id"]),))
            wid = int(body["id"])
            recompute_watch(con, wid)
        else:
            cur = con.execute("""INSERT INTO watch(name,category,keywords,exclude,max_price,resale_value,min_qty,
                                 sources,active,created) VALUES (?,?,?,?,?,?,?,?,?,?)""", vals + (now_iso(),))
            wid = cur.lastrowid
    return {"id": wid}


class Handler(BaseHTTPRequestHandler):
    server_version = "DealFinder/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, INDEX_HTML, "text/html")
            if u.path in ("/favicon.ico", "/icon.svg"):
                return self._send(200, ICON_SVG, "image/svg+xml")
            if u.path == "/manifest.json":
                return self._send(200, MANIFEST, "application/manifest+json")
            if u.path == "/api/state":
                return self._send(200, api_state())
            if u.path == "/api/deals":
                return self._send(200, api_deals(qs))
            self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/watch":
                return self._send(200, api_save_watch(body))
            if path == "/api/watch/delete":
                with db() as con:
                    con.execute("DELETE FROM deal WHERE watch_id=?", (int(body["id"]),))
                    con.execute("DELETE FROM watch WHERE id=?", (int(body["id"]),))
                return self._send(200, {"ok": True})
            if path == "/api/deal":
                with db() as con:
                    for f in ("starred", "dismissed"):
                        if f in body:
                            con.execute(f"UPDATE deal SET {f}=? WHERE id=?", (int(bool(body[f])), int(body["id"])))
                return self._send(200, {"ok": True})
            if path == "/api/deals/clear-dismissed":
                with db() as con:
                    con.execute("UPDATE deal SET dismissed=0")
                return self._send(200, {"ok": True})
            if path == "/api/scan":
                wid = body.get("watch_id")
                threading.Thread(target=run_scan, args=(int(wid) if wid else None,), daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                save_settings(body)
                with db() as con:
                    for (wid,) in con.execute("SELECT id FROM watch").fetchall():
                        recompute_watch(con, wid)
                return self._send(200, {"ok": True})
            if path == "/api/test-notify":
                ok = notify(get_settings(), "Deal Finder test", "Alerts are working 🎉", tags="tada")
                return self._send(200, {"ok": ok, "error": None if ok else "Set an ntfy topic first"})
            self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(400, {"error": str(e)})


ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0f1115"/>'
            '<path d="M14 30 30 14h18v18L32 48z" fill="#ffb020"/><circle cx="40" cy="22" r="4" fill="#0f1115"/></svg>')
MANIFEST = json.dumps({
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    "name": "Deal Finder", "short_name": "Deals", "start_url": "/", "display": "standalone",
    "background_color": "#0f1115", "theme_color": "#0f1115",
})

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1115"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg">
<title>Deal Finder</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--text:#e8eaef;--mute:#8b93a4;
--acc:#ffb020;--good:#2ecc71;--bad:#ff5c5c;--chip:#252a35;--r:14px}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select,textarea{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.92);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line);padding:10px 14px;display:flex;align-items:center;gap:10px}
.logo{font-weight:800;letter-spacing:.3px;font-size:18px}.logo b{color:var(--acc)}
.status{color:var(--mute);font-size:12px;flex:1;min-width:0;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.btn{background:var(--chip);border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer}
a.btn{color:var(--text);display:inline-block}
.btn.pri{background:var(--acc);color:#1a1200;border-color:var(--acc);font-weight:700}
.btn.danger{color:var(--bad)}.btn:disabled{opacity:.5}
nav{display:flex;gap:6px;padding:10px 14px 0}
nav button{flex:1;background:none;border:none;border-bottom:2px solid transparent;padding:8px;color:var(--mute);cursor:pointer;font-weight:600}
nav button.on{color:var(--text);border-color:var(--acc)}
main{padding:12px 14px 90px;max-width:1200px;margin:0 auto}
.layout{display:block}
aside{display:none}
@media (min-width:720px){
  nav .tab-watch{display:none}
  .layout{display:grid;grid-template-columns:260px 1fr;gap:16px}
  aside{display:block;position:sticky;top:64px;align-self:start;max-height:calc(100vh - 80px);overflow:auto}
  #watchView.view{display:none!important}
  .fab{display:none!important}
}
.chips{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;scrollbar-width:none}
.chip{flex:none;background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:13px;cursor:pointer;white-space:nowrap}
.chip.on{background:var(--acc);color:#1a1200;border-color:var(--acc);font-weight:700}
.toolbar{display:flex;gap:8px;align-items:center;margin:10px 0;flex-wrap:wrap}
.toolbar input[type=search]{flex:1;min-width:140px}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 10px;width:100%}
select{width:auto}
label.tog{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--mute);white-space:nowrap}
label.tog input{width:auto}
.deals{display:grid;gap:10px;grid-template-columns:1fr}
@media (min-width:1000px){.deals{grid-template-columns:1fr 1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:10px;display:grid;grid-template-columns:76px 1fr;gap:10px}
.thumb{width:76px;height:76px;border-radius:10px;background:var(--panel2) center/cover no-repeat;display:flex;align-items:center;justify-content:center;color:var(--mute);font-size:24px}
.ttl{font-weight:600;font-size:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:var(--text);text-decoration:none}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;align-items:center;font-size:12px;color:var(--mute)}
.tag{background:var(--chip);border-radius:6px;padding:2px 7px}
.tag.src-ebay{color:#7fb3ff}.tag.src-slickdeals{color:#ffb020}.tag.src-reddit{color:#ff8a5c}
.tag.bulk{background:#2b2410;color:var(--acc);font-weight:700}
.price{font-size:18px;font-weight:800;color:var(--text)}
.unit{font-size:12px;color:var(--mute)}
.profit{font-weight:800;border-radius:8px;padding:2px 8px;font-size:13px}
.profit.pos{background:rgba(46,204,113,.14);color:var(--good)}.profit.neg{background:rgba(255,92,92,.14);color:var(--bad)}
.profit.none{background:var(--chip);color:var(--mute);font-weight:500}
.acts{display:flex;gap:6px;margin-top:8px}
.acts .btn{padding:5px 10px;font-size:13px}
.star.on{color:var(--acc);border-color:var(--acc)}
.empty{color:var(--mute);text-align:center;padding:40px 10px}
.wlist{display:grid;gap:8px}
.witem{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px;cursor:pointer}
.witem.on{border-color:var(--acc)}
.witem .n{font-weight:700;display:flex;justify-content:space-between;gap:6px}
.witem .s{font-size:12px;color:var(--mute);margin-top:2px}
.witem.off{opacity:.5}
.cnt{background:var(--chip);border-radius:999px;padding:0 8px;font-size:12px;color:var(--mute);font-weight:600}
h3{margin:4px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.8px;color:var(--mute)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px;margin-bottom:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
.field>span{font-size:12px;color:var(--mute)}
.hint{font-size:12px;color:var(--mute);margin:-4px 0 10px}
.log{font-size:12px;font-family:ui-monospace,monospace;max-height:260px;overflow:auto}
.log div{padding:3px 0;border-bottom:1px solid var(--line)}
.log .error{color:var(--bad)}.log .skipped{color:var(--mute)}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:18px;padding:16px;width:min(520px,94vw)}
dialog::backdrop{background:rgba(0,0,0,.6)}
.row{display:flex;gap:8px;justify-content:flex-end;margin-top:6px}
.srcs{display:flex;gap:12px;flex-wrap:wrap}
.view{display:none}.view.on{display:block}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none;z-index:20}
.toast.on{opacity:1}
.fab{position:fixed;right:16px;bottom:18px;z-index:6;border-radius:999px;padding:14px 18px;box-shadow:0 6px 20px rgba(0,0,0,.4)}
</style></head><body>
<header>
  <div class="logo">Deal<b>Finder</b></div>
  <div class="status" id="status">…</div>
  <button class="btn pri" id="scanBtn" onclick="scanNow()">Scan now</button>
</header>
<nav>
  <button class="on" data-v="dealsView" onclick="show(this)">Deals</button>
  <button class="tab-watch" data-v="watchView" onclick="show(this)">Watchlist</button>
  <button data-v="settingsView" onclick="show(this)">Settings</button>
</nav>
<main>
  <section id="dealsView" class="view on">
    <div class="layout">
      <aside><h3>Watchlist</h3><div class="wlist" id="wlistSide"></div>
        <button class="btn" style="width:100%;margin-top:8px" onclick="editWatch()">+ Add item to watch</button></aside>
      <div>
        <div class="chips" id="catChips"></div>
        <div class="toolbar">
          <input type="search" id="q" placeholder="Filter deals…" oninput="debounceLoad()">
          <select id="sort" onchange="loadDeals()">
            <option value="new">Newest</option><option value="profit">Best profit</option>
            <option value="price">Lowest price</option><option value="unit">Lowest unit price</option>
          </select>
          <label class="tog"><input type="checkbox" id="bulk" onchange="loadDeals()"> Bulk only</label>
          <label class="tog"><input type="checkbox" id="starred" onchange="loadDeals()"> ★ Saved</label>
        </div>
        <div class="deals" id="deals"></div>
      </div>
    </div>
  </section>

  <section id="watchView" class="view">
    <div class="wlist" id="wlistMain"></div>
    <button class="btn" style="width:100%;margin-top:10px" onclick="editWatch()">+ Add item to watch</button>
  </section>

  <section id="settingsView" class="view">
    <div class="panel">
      <h3>Scanning & profit</h3>
      <div class="grid2">
        <label class="field"><span>Auto-scan</span><select id="s_scan_minutes" style="width:100%">
          <option value="0">Off — only when I tap Scan now</option><option value="15">Every 15 min</option>
          <option value="30">Every 30 min</option><option value="60">Every hour</option><option value="180">Every 3 hours</option></select></label>
        <label class="field"><span>Resale fee %</span><input id="s_fee_percent" type="number" step="0.01"></label>
      </div>
      <p class="hint">Fee % is taken off your resale value when estimating profit (eBay is roughly 13%).</p>
      <label class="tog"><input type="checkbox" id="s_show_images"> Show product images (turn off to save mobile data)</label>
      <p class="hint" id="manualNote" style="display:none;margin-top:8px">Started with <b>--manual</b>, so auto-scan stays off regardless of this setting.</p>
    </div>
    <div class="panel">
      <h3>Phone alerts (ntfy)</h3>
      <p class="hint">Install the free <b>ntfy</b> app on your phone, subscribe to the topic below, and you'll get a push when a deal clears your profit threshold.</p>
      <div class="grid2">
        <label class="field"><span>Topic (make it hard to guess)</span><input id="s_ntfy_topic" placeholder="dealfinder-x7k2q9"></label>
        <label class="field"><span>Alert if profit ≥ $</span><input id="s_min_profit_alert" type="number"></label>
      </div>
      <label class="field"><span>ntfy server</span><input id="s_ntfy_server"></label>
      <button class="btn" onclick="testNotify()">Send test alert</button>
    </div>
    <div class="panel">
      <h3>eBay API keys</h3>
      <p class="hint">Free at developer.ebay.com → Application Keys → <b>Production</b> keyset. Without these, eBay is skipped.</p>
      <label class="field"><span>App ID (Client ID)</span><input id="s_ebay_client_id"></label>
      <label class="field"><span>Cert ID (Client Secret)</span><input id="s_ebay_client_secret" type="password"></label>
    </div>
    <div class="panel">
      <h3>Subreddits per category</h3>
      <p class="hint">Comma-separated, without the r/.</p>
      <div id="subFields"></div>
    </div>
    <div class="row" style="justify-content:flex-start"><button class="btn pri" onclick="saveSettings()">Save settings</button>
      <button class="btn" onclick="restoreDismissed()">Restore dismissed deals</button></div>
    <div class="panel" style="margin-top:12px"><h3>Scan log</h3><div class="log" id="log"></div></div>
  </section>
</main>
<button class="btn pri fab" onclick="editWatch()" id="fab">+ Watch</button>

<dialog id="wdlg"><form method="dialog" id="wform" onsubmit="return false">
  <h3 id="wtitle">Watch an item</h3>
  <input type="hidden" name="id">
  <label class="field"><span>Name</span><input name="name" placeholder="e.g. GameCube controller lots"></label>
  <div class="grid2">
    <label class="field"><span>Category</span><select name="category" style="width:100%"></select></label>
    <label class="field"><span>Min quantity (bulk)</span><input name="min_qty" type="number" min="1" value="1"></label>
  </div>
  <label class="field"><span>Keywords (all must appear in title)</span><input name="keywords" required placeholder="gamecube controller lot"></label>
  <label class="field"><span>Exclude words (comma-separated)</span><input name="exclude" placeholder="repro, broken, for parts"></label>
  <div class="grid2">
    <label class="field"><span>Max total price $</span><input name="max_price" type="number" step="0.01"></label>
    <label class="field"><span>Resale value per unit $</span><input name="resale_value" type="number" step="0.01" placeholder="what 1 sells for"></label>
  </div>
  <p class="hint">Resale value per unit is what one item sells for (check eBay sold listings or PriceCharting for retro games). It's how profit gets calculated.</p>
  <div class="field"><span>Sources</span><div class="srcs" id="wsrcs"></div></div>
  <label class="tog" style="margin-bottom:10px"><input type="checkbox" name="active" checked> Active</label>
  <div class="row">
    <button class="btn danger" id="wdel" type="button" onclick="deleteWatch()">Delete</button>
    <span style="flex:1"></span>
    <button class="btn" type="button" onclick="wdlg.close()">Cancel</button>
    <button class="btn pri" type="button" onclick="saveWatch()">Save</button>
  </div>
</form></dialog>
<div class="toast" id="toast"></div>

<script>
let ST=null, CAT="", WATCH="", timer=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=v=>v==null?"":"$"+Number(v).toFixed(2);
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||r.status); return j;}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("on"),2200);}
function ago(iso){if(!iso)return"";const s=(Date.now()-new Date(iso))/1000;
  if(s<90)return"just now";if(s<3600)return Math.round(s/60)+"m ago";if(s<86400)return Math.round(s/3600)+"h ago";return Math.round(s/86400)+"d ago";}
function show(btn){document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b===btn));
  document.querySelectorAll(".view").forEach(v=>v.classList.toggle("on",v.id===btn.dataset.v));
  $("#fab").style.display=btn.dataset.v==="settingsView"?"none":"";}

async function loadState(){
  ST=await api("/api/state"); const s=ST.status;
  const used=s.last_bytes!=null?` · ${s.last_bytes>1048576?(s.last_bytes/1048576).toFixed(1)+" MB":Math.max(1,Math.round(s.last_bytes/1024))+" KB"} used`:"";
  const nxt=s.auto&&s.next_scan_in!=null?` · next in ${Math.ceil(s.next_scan_in/60)}m`:" · auto-scan off";
  $("#status").textContent=s.scanning?"Scanning…":(s.last_scan?`Last scan ${ago(s.last_scan)}${used}${nxt}`:(s.auto?"First scan starting…":"Tap Scan now to search"));
  $("#scanBtn").disabled=s.scanning;
  $("#catChips").innerHTML=["",...ST.categories].map(c=>`<button class="chip ${c===CAT&&!WATCH?"on":""}" onclick="setCat('${c}')">${c||"All"}</button>`).join("");
  const wl=ST.watches.map(w=>`<div class="witem ${w.active?"":"off"} ${String(w.id)===WATCH?"on":""}" onclick="pickWatch(${w.id})">
     <div class="n"><span>${esc(w.name)}</span><span class="cnt">${w.deal_count}</span></div>
     <div class="s">${esc(w.category)} · ≤ ${w.max_price?money(w.max_price):"any"}${w.min_qty>1?` · qty ≥ ${w.min_qty}`:""}${w.resale_value?` · resale ${money(w.resale_value)}/ea`:" · <i>no resale value</i>"}</div>
     <div class="s"><a href="#" onclick="event.stopPropagation();editWatch(${w.id});return false" style="color:var(--acc)">Edit</a></div></div>`).join("")
     || `<div class="empty">Nothing on your watchlist yet.</div>`;
  $("#wlistSide").innerHTML=wl; $("#wlistMain").innerHTML=wl;
  $("#log").innerHTML=ST.log.map(l=>`<div class="${l.status}">${new Date(l.ts).toLocaleTimeString()} · ${esc(l.source)} · ${esc(l.watch)} — ${esc(l.status)}: ${esc(l.message)}</div>`).join("")||"No scans yet.";
  if(!document.activeElement||!document.activeElement.id?.startsWith("s_")) fillSettings();
}
function setCat(c){CAT=c;WATCH="";loadState();loadDeals();}
function pickWatch(id){WATCH=String(id)===WATCH?"":String(id);CAT="";
  if(innerWidth<720) show(document.querySelector('nav button[data-v=dealsView]'));
  loadState();loadDeals();}
let dT;function debounceLoad(){clearTimeout(dT);dT=setTimeout(loadDeals,250);}

async function loadDeals(){
  const p=new URLSearchParams({sort:$("#sort").value});
  if(CAT)p.set("category",CAT); if(WATCH)p.set("watch",WATCH);
  if($("#bulk").checked)p.set("bulk","1"); if($("#starred").checked)p.set("starred","1");
  if($("#q").value)p.set("q",$("#q").value);
  const deals=await api("/api/deals?"+p);
  $("#deals").innerHTML=deals.map(card).join("")||`<div class="empty">No deals yet. Tap <b>Scan now</b>, or add items to your watchlist.<br><br>Tip: set a <b>resale value</b> on each item to see estimated profit.</div>`;
}
function card(d){
  const unit=d.qty>1&&d.price!=null?`<span class="unit">${d.qty} units · ${money(d.price/d.qty)}/ea</span>`:"";
  let pr=`<span class="profit none">set resale value</span>`;
  if(d.profit!=null) pr=`<span class="profit ${d.profit>=0?"pos":"neg"}">${d.profit>=0?"+":""}${money(d.profit)} ${d.margin!=null?`(${d.margin}%)`:""}</span>`;
  else if(d.resale_value) pr=`<span class="profit none">price unknown</span>`;
  const showImg=ST.settings.show_images&&d.image;
  const img=showImg?`style="background-image:url('${esc(d.image)}')"`:"";
  return `<div class="card"><a class="thumb" ${img} href="${esc(d.url)}" target="_blank" rel="noopener">${showImg?"":"🏷️"}</a>
   <div><a class="ttl" href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>
    <div class="meta"><span class="price">${d.price!=null?money(d.price):"price ?"}</span>${d.shipping?`<span>+${money(d.shipping)} ship</span>`:""} ${unit}</div>
    <div class="meta">${pr}<span class="tag src-${d.source}">${d.source}</span>${d.bulk?`<span class="tag bulk">BULK</span>`:""}<span>${esc(d.watch_name)}</span><span>${ago(d.posted||d.found_at)}</span></div>
    <div class="acts"><button class="btn star ${d.starred?"on":""}" onclick="mark(${d.id},{starred:${d.starred?0:1}})">★</button>
     <button class="btn" onclick="mark(${d.id},{dismissed:1})">Hide</button>
     <a class="btn" href="${esc(d.url)}" target="_blank" rel="noopener" style="text-decoration:none">Open ↗</a></div></div></div>`;
}
async function mark(id,b){await api("/api/deal",{id,...b});loadDeals();loadState();}
async function scanNow(){await api("/api/scan",WATCH?{watch_id:WATCH}:{});toast(WATCH?"Scanning this item…":"Scanning everything…");
  $("#scanBtn").disabled=true; poll();}
function poll(){let n=0;const h=setInterval(async()=>{await loadState();await loadDeals();if(!ST.status.scanning||++n>120)clearInterval(h);},3000);}

function editWatch(id){
  const w=ST.watches.find(x=>x.id===id)||{category:CAT||ST.categories[0],min_qty:1,sources:ST.sources,active:1};
  const f=$("#wform"); f.reset();
  f.category.innerHTML=ST.categories.map(c=>`<option>${c}</option>`).join("");
  for(const k of["id","name","category","keywords","exclude","max_price","resale_value","min_qty"]) f[k].value=w[k]??"";
  f.active.checked=!!w.active;
  $("#wsrcs").innerHTML=ST.sources.map(s=>`<label class="tog"><input type="checkbox" value="${s}" ${w.sources.includes(s)?"checked":""}> ${s}</label>`).join("");
  $("#wtitle").textContent=id?"Edit watch":"Watch an item"; $("#wdel").style.display=id?"":"none";
  $("#wdlg").showModal();
}
async function saveWatch(){
  const f=$("#wform"); const b={};
  for(const k of["id","name","category","keywords","exclude","max_price","resale_value","min_qty"]) b[k]=f[k].value;
  b.active=f.active.checked; b.sources=[...document.querySelectorAll("#wsrcs input:checked")].map(i=>i.value);
  if(!b.keywords.trim()){toast("Keywords are required");return;}
  try{const r=await api("/api/watch",b);$("#wdlg").close();toast("Saved");
    if(!b.id) await api("/api/scan",{watch_id:r.id}), poll();
    loadState();loadDeals();}catch(e){toast(e.message)}
}
async function deleteWatch(){const id=$("#wform").id.value;if(!confirm("Delete this watch and its deals?"))return;
  await api("/api/watch/delete",{id});$("#wdlg").close();if(WATCH===id)WATCH="";loadState();loadDeals();}

function fillSettings(){const s=ST.settings;
  for(const k of["fee_percent","ntfy_topic","min_profit_alert","ntfy_server","ebay_client_id","ebay_client_secret"]) $("#s_"+k).value=s[k]??"";
  const sel=$("#s_scan_minutes"), m=String(Number(s.scan_minutes)||0);
  if(![...sel.options].some(o=>o.value===m)) sel.add(new Option(`Every ${m} min`,m));
  sel.value=m; $("#s_show_images").checked=!!s.show_images;
  $("#manualNote").style.display=ST.status.manual_flag?"":"none";
  $("#subFields").innerHTML=ST.categories.map(c=>`<label class="field"><span>${c}</span><input data-cat="${c}" value="${esc(s.subreddits[c]||"")}"></label>`).join("");}
async function saveSettings(){const b={subreddits:{}};
  for(const k of["scan_minutes","fee_percent","ntfy_topic","min_profit_alert","ntfy_server","ebay_client_id","ebay_client_secret"]) b[k]=$("#s_"+k).value;
  b.show_images=$("#s_show_images").checked;
  document.querySelectorAll("#subFields input").forEach(i=>b.subreddits[i.dataset.cat]=i.value);
  try{await api("/api/settings",b);toast("Settings saved");document.activeElement.blur();loadState();loadDeals();}catch(e){toast(e.message)}}
async function testNotify(){await saveSettings();try{const r=await api("/api/test-notify",{});toast(r.ok?"Test alert sent — check your phone":r.error)}catch(e){toast(e.message)}}
async function restoreDismissed(){await api("/api/deals/clear-dismissed",{});toast("Hidden deals restored");loadDeals();}

loadState().then(loadDeals); setInterval(loadState,30000);
</script></body></html>"""


# --------------------------------------------------------------------------- main
def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Deal Finder")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DEALFINDER_PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("DEALFINDER_HOST", "0.0.0.0"))
    ap.add_argument("--scan-once", action="store_true", help="run one scan, print results, exit")
    ap.add_argument("--manual", action="store_true",
                    help="never scan automatically; only when you tap Scan now (good for phones)")
    args = ap.parse_args()
    init_db()

    if args.scan_once:
        n = run_scan()
        print(f"New deals: {n}")
        with db() as con:
            for r in con.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 40"):
                print(f"  [{r['status']}] {r['source']:<10} {r['watch']:<22} {r['message']}")
        return

    STATE["manual"] = args.manual
    schedule_next(first=True)
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    msg = f"Deal Finder running:\n  open: http://localhost:{args.port}\n"
    if args.host not in ("127.0.0.1", "localhost"):
        msg += f"  other devices on your Wi-Fi: http://{lan_ip()}:{args.port}\n"
    msg += ("  auto-scan: OFF (tap Scan now)\n" if auto_minutes() <= 0 else f"  auto-scan: every {auto_minutes():g} min\n")
    print(msg + "Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
