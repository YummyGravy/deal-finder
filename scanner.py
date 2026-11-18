#!/usr/bin/env python3
"""
Cloud scanner for Deal Finder. Runs inside GitHub Actions (or anywhere with Python 3.9+).

Modes (set with the MODE environment variable):
  search     one-off search for QUERY            -> docs/data/search.json
  watchlist  scan every active watchlist item    -> docs/data/deals.json

Other env vars: QUERY, CATEGORY, MAX_PRICE, MIN_QTY, EXCLUDE, SOURCES (comma list),
                EBAY_CLIENT_ID, EBAY_CLIENT_SECRET (optional, from repo secrets)
Standard library only.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import dealfinder as core

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "docs", "data")
WATCHLIST = os.path.join(DATA, "watchlist.json")
DEALS = os.path.join(DATA, "deals.json")
SEARCH = os.path.join(DATA, "search.json")
KEEP_DAYS = 30
MAX_DEALS = 600


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def settings_for(wl):
    s = json.loads(json.dumps(core.DEFAULT_SETTINGS))
    s["subreddits"].update((wl.get("settings") or {}).get("subreddits") or {})
    s["ebay_client_id"] = os.environ.get("EBAY_CLIENT_ID", "")
    s["ebay_client_secret"] = os.environ.get("EBAY_CLIENT_SECRET", "")
    return s


def run_item(item, settings, log):
    """Search every source for one watch item. Returns matching deals (dicts)."""
    watch = {
        "keywords": item.get("keywords", ""),
        "category": item.get("category") or "Electronics",
        "exclude": item.get("exclude", ""),
        "max_price": num(item.get("max_price")),
    }
    min_qty = int(num(item.get("min_qty")) or 1)
    out = []
    for src in item.get("sources") or core.SOURCES:
        fn = core.SOURCE_FUNCS.get(src)
        if not fn:
            continue
        label = item.get("name") or watch["keywords"]
        try:
            results = fn(watch, settings)
        except core.SkipSource as e:
            msg = "no eBay keys (add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET as repo secrets)" if src == "ebay" else str(e)
            log.append({"source": src, "item": label, "status": "skipped", "msg": msg})
            continue
        except urllib.error.HTTPError as e:
            hint = " (site is blocking cloud servers)" if e.code in (403, 429) else ""
            log.append({"source": src, "item": label, "status": "error", "msg": f"HTTP {e.code}{hint}"})
            continue
        except Exception as e:
            log.append({"source": src, "item": label, "status": "error", "msg": f"{type(e).__name__}: {e}"[:200]})
            continue
        kept = 0
        for r in results:
            if not r.get("url") or not core.title_matches(r["title"], watch["keywords"], watch["exclude"]):
                continue
            if watch["max_price"] and r["price"] is not None and r["price"] > watch["max_price"]:
                continue
            qty = core.parse_qty(r["title"])
            if qty < min_qty:
                continue
            title = r["title"] + (f"  · {r['suffix']}" if r.get("suffix") else "")
            out.append({
                "id": hashlib.sha1(r["url"].encode()).hexdigest()[:12],
                "source": src, "title": title, "url": r["url"],
                "price": r["price"], "shipping": r.get("shipping"), "qty": qty,
                "bulk": core.is_bulk(r["title"], qty), "image": r.get("image"), "posted": r.get("posted"),
            })
            kept += 1
        log.append({"source": src, "item": label, "status": "ok", "msg": f"{len(results)} results, {kept} matched"})
        time.sleep(1)
    return out


def mode_search():
    wl = load(WATCHLIST, {})
    item = {
        "name": os.environ.get("QUERY", "").strip(),
        "keywords": os.environ.get("QUERY", "").strip(),
        "category": os.environ.get("CATEGORY") or "Electronics",
        "max_price": os.environ.get("MAX_PRICE"),
        "min_qty": os.environ.get("MIN_QTY") or 1,
        "exclude": os.environ.get("EXCLUDE", ""),
        "sources": [s.strip() for s in (os.environ.get("SOURCES") or ",".join(core.SOURCES)).split(",") if s.strip()],
    }
    log = []
    results = run_item(item, settings_for(wl), log) if item["keywords"] else []
    if not item["keywords"]:
        log.append({"source": "-", "item": "-", "status": "error", "msg": "empty search"})
    seen, uniq = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"]); uniq.append(r)
    uniq.sort(key=lambda r: (r["price"] is None, (r["price"] or 0) / max(1, r["qty"])))
    save(SEARCH, {"query": item["keywords"], "params": {k: item[k] for k in ("category", "max_price", "min_qty", "exclude", "sources")},
                  "run_at": now(), "run_id": os.environ.get("GITHUB_RUN_ID"), "results": uniq, "log": log})
    print(f"search '{item['keywords']}': {len(uniq)} results")
    for l in log:
        print(f"  [{l['status']}] {l['source']}: {l['msg']}")


def mode_watchlist():
    wl = load(WATCHLIST, {"items": []})
    settings = settings_for(wl)
    store = load(DEALS, {"deals": []})
    existing = {(d["watch_id"], d["url"]): d for d in store.get("deals", [])}
    log, new = [], 0
    stamp = now()
    for item in wl.get("items", []):
        if not item.get("active", True):
            continue
        for d in run_item(item, settings, log):
            key = (item["id"], d["url"])
            if key in existing:
                existing[key].update(price=d["price"], shipping=d["shipping"], last_seen=stamp)
            else:
                d.update(watch_id=item["id"], first_seen=stamp, last_seen=stamp)
                existing[key] = d
                new += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    ids = {i["id"] for i in wl.get("items", [])}
    deals = [d for d in existing.values() if d["watch_id"] in ids and d.get("last_seen", "") >= cutoff]
    deals.sort(key=lambda d: d.get("first_seen", ""), reverse=True)
    save(DEALS, {"updated": stamp, "run_id": os.environ.get("GITHUB_RUN_ID"), "new": new,
                 "deals": deals[:MAX_DEALS], "log": log})
    print(f"watchlist scan: {new} new deals, {len(deals)} kept")
    for l in log:
        print(f"  [{l['status']}] {l['source']:<10} {l['item']}: {l['msg']}")


if __name__ == "__main__":
    mode = (os.environ.get("MODE") or "watchlist").strip()
    (mode_search if mode == "search" else mode_watchlist)()
    sys.exit(0)
