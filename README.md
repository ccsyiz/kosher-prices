# Evergreen vs Wesley

A website comparing prices at Evergreen Kosher (Uptown, Pomona) and Wesley Kosher (Monsey).

## How it works

- `scraper/scrape.py` opens each store's online shop, reads its full product list and current sales, and matches products that both stores sell (they share product IDs because both shops run on the same platform).
- It writes `data/compare.json`, which the website reads, plus one small file per day in `data/history/` so price changes can be charted later.
- `.github/workflows/update-prices.yml` runs the scraper every morning on GitHub's computers and saves the results.
- `index.html` is the website. GitHub Pages serves it for free.

Until the first daily run, `data/compare.json` holds a 272-product example pulled on October 15, 2026.

## Run it yourself

```
pip install playwright
python -m playwright install chromium
python scraper/scrape.py
```

## Be a good neighbor

The scraper runs once a day and pauses between requests. Prices are shown with product names only; no store photos are copied. Online prices can differ from shelf prices.
