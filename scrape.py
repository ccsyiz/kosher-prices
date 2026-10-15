#!/usr/bin/env python3
"""
Daily price scraper for Evergreen Kosher (Uptown, Pomona) and Wesley Kosher (Monsey).

Both stores' online shops run on the same platform (Self Point), so one scraper
handles both. It opens each store's website in a real (headless) browser, then
asks the site for its product list the same way the website itself does, a
page of 500 products at a time, with a pause between pages.

Products are matched across stores by the platform's shared product ID, so a
can of Kedem grape juice has the same ID at both stores.

Output (all under ../data):
  compare.json            matched products with both stores' prices (the website reads this)
  stores/<store>.json     every product at each store, matched or not
  history/YYYY-MM-DD.json one small file per day: {productId: [evergreen, wesley]}

Run:
  pip install playwright
  playwright install chromium
  python scraper/scrape.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STORES = [
    {
        "key": "evergreen",
        "name": "Evergreen Uptown",
        "site": "https://www.shopevergreenkosher.com",
        "retailer": 1120,
        "branch": 2584,  # Uptown, 1581 US-202, Pomona. (883 = Monsey, 2801 = Lakewood)
    },
    {
        "key": "wesley",
        "name": "Wesley Kosher",
        "site": "https://www.wesleykosheronline.com",
        "retailer": 1068,
        "branch": 96,  # 455 Route 306, Monsey
    },
]

PAGE_SIZE = 500
PAUSE_MS = 1000          # wait between pages so we don't hammer the site
DATA = Path(__file__).resolve().parent.parent / "data"

# Hebrew unit names the platform sometimes uses -> English
UNITS = {
    "אוז": "oz", "גרם": "g", "ליטר": "L", 'מ"ל': "ml", "גאלון": "gal",
    "רבע גלון": "qt", "ק\"ג": "kg", "יח'": "ct",
}

# This runs inside the store's web page, so requests look exactly like the site's own.
FETCH_CATALOG_JS = """
async ({retailer, branch, pageSize, pauseMs}) => {
  const filters = {must: {exists: ["family.id", "family.categoriesPaths.id", "branch.regularPrice"],
                          term: {"branch.isActive": true, "branch.isVisible": true}},
                   mustNot: {term: {"branch.regularPrice": 0}}};
  const base = `/v2/retailers/${retailer}/branches/${branch}`;
  const rows = [];
  for (let from = 0; ; from += pageSize) {
    const url = `${base}/products?appId=4&filters=${encodeURIComponent(JSON.stringify(filters))}&from=${from}&size=${pageSize}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`products page at ${from}: HTTP ${res.status}`);
    const products = (await res.json()).products || [];
    for (const p of products) {
      const fam = p.family || {};
      const path = (fam.categoriesPaths || [])[fam.mainCategoryPathIndex || 0] || (fam.categoriesPaths || [])[0] || [];
      rows.push({
        productId: p.productId,
        storeItemId: p.id,
        name: p.localName || p.names?.[2]?.short || p.names?.[2]?.long || "",
        brand: p.brand?.names?.[2] || "",
        size: p.weight ?? null,
        unit: p.unitOfMeasure?.defaultName || "",
        byWeight: !!p.isWeighable,
        price: p.branch?.regularPrice ?? null,
        category: path[0]?.names?.[2] || "",
        subcategory: path[1]?.names?.[2] || "",
      });
    }
    if (products.length < pageSize) break;
    await new Promise(r => setTimeout(r, pauseMs));
  }

  // Current sales. "Only $X" becomes a sale price; "2 for $5"-style deals also
  // record the per-item price ("each") and how many you must buy ("qty").
  const now = new Date().toISOString();
  const sf = {must: {lessThan: {startDate: now}, greaterThan: {endDate: now},
                     term: {displayOnWeb: true, isCoupon: false}}};
  const sales = {};
  for (let from = 0; ; from += 100) {
    const res = await fetch(`${base}/specials?appId=4&filters=${encodeURIComponent(JSON.stringify(sf))}&from=${from}&size=100`);
    if (!res.ok) break;
    const specials = (await res.json()).specials || [];
    for (const s of specials) {
      const label = s.names?.[2]?.promotionTag || s.description || "";
      const ends = s.endDate || null;
      for (const lvl of s.levels || []) {
        const gift = (lvl.gifts || [])[0];
        for (const buy of lvl.purchases || []) {
          for (const id of buy.products || []) {
            // gift.type 1 = a fixed price for buy.total items ("Only $3.89", "2 for $3")
            const fixed = gift && gift.type === 1 && buy.type === 1 && buy.total >= 1;
            sales[id] = {label, ends,
                         price: fixed && buy.total === 1 ? gift.total : null,
                         each: fixed && buy.total > 1 ? Math.round(gift.total / buy.total * 100) / 100 : null,
                         qty: fixed ? buy.total : null};
          }
        }
      }
    }
    if (specials.length < 100) break;
    await new Promise(r => setTimeout(r, pauseMs));
  }
  return {rows, sales};
}
"""


def clean(row, sales):
    row["unit"] = UNITS.get(row["unit"], row["unit"])
    if isinstance(row["size"], float):
        row["size"] = round(row["size"], 2)
    sale = sales.get(str(row["storeItemId"])) or sales.get(row["storeItemId"])
    if sale:
        row["sale"] = sale
    return row


def scrape_store(browser, store):
    page = browser.new_page()
    page.goto(store["site"] + "/", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(5000)  # let the site finish its own start-up checks
    result = page.evaluate(
        FETCH_CATALOG_JS,
        {"retailer": store["retailer"], "branch": store["branch"],
         "pageSize": PAGE_SIZE, "pauseMs": PAUSE_MS},
    )
    page.close()
    rows = [clean(r, result["sales"]) for r in result["rows"] if r.get("productId") and r.get("price")]
    print(f"{store['name']}: {len(rows)} products, {len(result['sales'])} items on sale")
    return rows


def effective(row):
    """Price a shopper actually pays today: the sale price if there's a simple one."""
    sale = row.get("sale") or {}
    return sale.get("price") or row["price"]


def build_compare(by_store):
    ev = {r["productId"]: r for r in by_store["evergreen"]}
    we = {r["productId"]: r for r in by_store["wesley"]}
    items = []
    for pid in ev.keys() & we.keys():
        e, w = ev[pid], we[pid]
        items.append({
            "id": pid,
            "name": e["name"] or w["name"],
            "brand": e["brand"] or w["brand"],
            "size": e["size"] if e["size"] not in (None, 0) else w["size"],
            "unit": e["unit"] or w["unit"],
            "byWeight": e["byWeight"] or w["byWeight"],
            "category": e["category"] or w["category"],
            "subcategory": e["subcategory"] or w["subcategory"],
            "evergreen": {"price": e["price"], "sale": e.get("sale"), "pays": effective(e)},
            "wesley": {"price": w["price"], "sale": w.get("sale"), "pays": effective(w)},
        })
    items.sort(key=lambda x: (x["category"], x["name"]))
    return items


def main():
    from playwright.sync_api import sync_playwright

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_store = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for store in STORES:
            for attempt in range(3):
                try:
                    by_store[store["key"]] = scrape_store(browser, store)
                    break
                except Exception as e:  # network hiccup, site slow, etc.
                    print(f"{store['name']} attempt {attempt + 1} failed: {e}")
                    time.sleep(30)
            else:
                sys.exit(f"Giving up on {store['name']}; keeping yesterday's data.")
        browser.close()

    # Sanity check: a tiny catalog means something went wrong; don't overwrite good data.
    for key, rows in by_store.items():
        if len(rows) < 1000:
            sys.exit(f"Only {len(rows)} products for {key}; not saving.")

    items = build_compare(by_store)
    (DATA / "stores").mkdir(parents=True, exist_ok=True)
    (DATA / "history").mkdir(parents=True, exist_ok=True)

    for key, rows in by_store.items():
        (DATA / "stores" / f"{key}.json").write_text(json.dumps(rows, ensure_ascii=False))

    meta = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "stores": {s["key"]: s["name"] for s in STORES},
            "counts": {k: len(v) for k, v in by_store.items()}, "matched": len(items)}
    (DATA / "compare.json").write_text(json.dumps({"meta": meta, "items": items}, ensure_ascii=False))

    history = {str(i["id"]): [i["evergreen"]["pays"], i["wesley"]["pays"]] for i in items}
    (DATA / "history" / f"{today}.json").write_text(json.dumps(history))
    print(f"Matched {len(items)} products. Saved to {DATA}")


if __name__ == "__main__":
    main()
