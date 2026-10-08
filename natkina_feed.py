#!/usr/bin/env python3
"""
NATKINA -> Awin product feed (free, no Shopify app).

Reads the public Shopify catalogue (https://natkina.com/products.json, all pages),
writes one row per variant to:
  public/awin.csv          Awin CSV feed (upload via URL in Awin: Toolbox > My Product Feeds)
  public/google.xml        Google Shopping RSS feed (same data, if Awin/partners prefer Google format)
  public/report.txt        data-quality report (what was fixed with a fallback, what to fix in Shopify)

Run:   python natkina_feed.py
Test:  python natkina_feed.py --input sample.json
Only standard library is used.
"""
import csv, html, io, json, re, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

SHOP = "https://natkina.com"
CURRENCY = "CHF"            # base currency of products.json prices — verify once in Shopify settings
BRAND = "NATKINA"           # default brand; Shopify vendor is used per product ("Natkina" -> "NATKINA")
# Extra currencies: one feed per country, prices taken from Shopify Markets (Storefront API),
# exactly as a visitor from that country sees them. Base CHF feed is always built from products.json.
# Format: "country code": "currency" (currency is only used for file names / sanity check).
MARKETS = {
    "DE": "EUR",   # also AT, FR, IT, NL... if they use the same EUR prices
    "GB": "GBP",
    "US": "USD",
    "PL": "PLN",
}
# Storefront API token (free): Shopify Admin > Settings > Apps > Develop apps > create app >
# Storefront API scopes: unauthenticated_read_product_listings, unauthenticated_read_product_inventory >
# install > copy "Storefront API access token". Leave empty to try tokenless access first.
STOREFRONT_TOKEN = ""
STOREFRONT_API = SHOP + "/api/2026-04/graphql.json"
INCLUDE_OUT_OF_STOCK = True # full feed: sold-out variants stay in with in_stock=0 / out_of_stock
OUT = Path(__file__).parent / "public"

# Not products for affiliates: excluded from the feed (listed in report.txt)
EXCLUDE_TYPES = {"gift card", "gift cards", "giftcard"}
EXCLUDE_HANDLE_WORDS = ("donation", "gift-card", "giftcard")
EXCLUDE_TITLE_START = ("support for",)

# Shopify product_type -> clean category (fixes singular/plural and odd types)
TYPE_FIX = {"necklace": "Necklaces", "ring": "Rings", "pendant": "Pendants", "earring": "Earrings",
            "bracelet": "Bracelets", "brooch": "Brooches", "high-jewelry": "", "ticket": "", "": ""}
# guessed from the product handle when the type is empty or unclear
HANDLE_HINTS = (("choker", "Necklaces"), ("necklace", "Necklaces"), ("pendant", "Pendants"),
                ("bracelet", "Bracelets"), ("bangle", "Bracelets"), ("earring", "Earrings"),
                ("stud", "Earrings"), ("letter", "Pendants"), ("ring", "Rings"))

# clean category -> Google product category (ID)
GOOGLE_CAT = {
    "earrings": "194", "necklaces": "196", "rings": "200", "bracelets": "191",
    "pendants": "192", "charms": "192", "anklets": "189", "brooches": "197",
    "body jewelry": "190", "watches": "201", "hair pins": "171",
}
GOOGLE_CAT_DEFAULT = "188"  # Apparel & Accessories > Jewelry


def fetch_all():
    products, page = [], 1
    while True:
        url = f"{SHOP}/products.json?limit=250&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": "natkina-feed/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    batch = json.load(r)["products"]
                break
            except Exception as e:  # 429 / network: back off and retry
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
        if not batch:
            return products
        products += batch
        page += 1
        time.sleep(1)


PRICE_QUERY = """
query($cursor: String, $country: CountryCode!) @inContext(country: $country) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { variants(first: 100) { nodes {
      id availableForSale
      price { amount currencyCode }
      compareAtPrice { amount currencyCode }
    } } }
  }
}"""


def fetch_market_prices(country):
    """{variant_id: (price, compare_at, currency, available)} for one country, from Shopify Markets."""
    out, cursor = {}, None
    headers = {"Content-Type": "application/json", "User-Agent": "natkina-feed/1.0"}
    if STOREFRONT_TOKEN:
        headers["X-Shopify-Storefront-Access-Token"] = STOREFRONT_TOKEN
    while True:
        body = json.dumps({"query": PRICE_QUERY, "variables": {"cursor": cursor, "country": country}}).encode()
        req = urllib.request.Request(STOREFRONT_API, data=body, headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:300])
        prod = data["data"]["products"]
        for node in prod["nodes"]:
            for v in node["variants"]["nodes"]:
                vid = v["id"].rsplit("/", 1)[-1]
                cap = v.get("compareAtPrice")
                out[vid] = (float(v["price"]["amount"]), float(cap["amount"]) if cap else None,
                            v["price"]["currencyCode"], v["availableForSale"])
        if not prod["pageInfo"]["hasNextPage"]:
            return out
        cursor = prod["pageInfo"]["endCursor"]
        time.sleep(0.5)


def localize(rows, prices, currency):
    """Copy of the base rows with this market's prices; variants without a market price are dropped."""
    res, missing = [], 0
    for r in rows:
        mp = prices.get(r["product_id"])
        if not mp:
            missing += 1
            continue
        price, cap, cur, avail = mp
        n = dict(r)
        n["search_price"] = f"{price:.2f}"
        n["rrp_price"] = f"{cap:.2f}" if cap and cap > price else ""
        n["currency"] = cur
        n["in_stock"] = "1" if avail else "0"
        res.append(n)
    return res, missing


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def option_map(product, variant):
    """Map NATKINA option names (Metal Color / Stone Color / Size...) to feed attributes."""
    out = {"material": "", "colour": "", "size": ""}
    for i, opt in enumerate(product.get("options", []), start=1):
        name = (opt.get("name") or "").lower()
        val = variant.get(f"option{i}") or ""
        if val in ("", "Default Title", "One Size"):
            continue
        if "metal" in name or "material" in name:
            out["material"] = val
        elif "color" in name or "colour" in name:
            out["colour"] = val
        elif "size" in name:
            out["size"] = val
    return out


def clean_type(p):
    raw = (p.get("product_type") or "").strip()
    t = TYPE_FIX.get(raw.lower(), raw)
    if not t:
        h = p["handle"].lower()
        t = next((cat for word, cat in HANDLE_HINTS if word in h), "Jewellery")
    return raw, t


def brand_of(p):
    v = (p.get("vendor") or "").strip()
    return BRAND if not v or v.upper() == BRAND else v


def excluded(p):
    h, t = p["handle"].lower(), (p.get("product_type") or "").strip().lower()
    if t in EXCLUDE_TYPES or any(w in h for w in EXCLUDE_HANDLE_WORDS):
        return True
    if p["title"].strip().lower().startswith(EXCLUDE_TITLE_START):
        return True
    return all(float(v["price"]) <= 0 for v in p.get("variants", []))


def build_rows(products, issues):
    rows = []
    vendors = set()
    names = {}
    for p in products:
        if excluded(p):
            issues["excluded"].append(f"{p['handle']} ({p['title'].strip()})")
            continue
        vendors.add(p.get("vendor"))
        brand = brand_of(p)
        images = [i["src"] for i in p.get("images", [])]
        desc = clean_text(p.get("body_html"))
        if not desc:
            issues["empty_description"].append(p["handle"])
        raw_type, ptype = clean_type(p)
        if not raw_type:
            issues["empty_product_type"].append(f"{p['handle']} -> {ptype}")
        if "copy" in p["handle"].lower():
            issues["copy_handles"].append(p["handle"])
        names.setdefault(p["title"].strip().lower(), []).append(p["handle"])
        gcat = GOOGLE_CAT.get(ptype.lower(), GOOGLE_CAT_DEFAULT)
        sizes = [option_map(p, v)["size"] for v in p.get("variants", [])]
        has_long_size = any(re.fullmatch(r"\d{2}", x or "") for x in sizes)
        if not images:
            issues["no_image"].append(p["handle"])
            continue  # Awin and Google both reject products without an image

        for v in p.get("variants", []):
            if not v.get("available") and not INCLUDE_OUT_OF_STOCK:
                continue
            attrs = option_map(p, v)
            title = p["title"].strip()
            if v.get("title") and v["title"] != "Default Title":
                title = f"{title} - {v['title']}"
            vdesc = desc or f"{p['title'].strip()} by {brand}. {ptype}. " + ", ".join(
                x for x in (attrs["material"], attrs["colour"] and f"stone: {attrs['colour']}", attrs["size"] and f"size: {attrs['size']}") if x)
            img = (v.get("featured_image") or {}).get("src") or images[0]
            alt_imgs = [i for i in images if i != img][:3]
            price = float(v["price"])
            cap = v.get("compare_at_price")
            rrp = float(cap) if cap and float(cap) > price else None
            if not v.get("sku"):
                issues["empty_sku"].append(f"{p['handle']} / {v['title']}")
            if has_long_size and re.fullmatch(r"\d", attrs["size"]):
                issues["suspicious_size"].append(f"{p['handle']} / {v['title']} (sku {v.get('sku')})")
            rows.append({
                "product_id": str(v["id"]),
                "parent_product_id": str(p["id"]),
                "product_name": title[:150],
                "description": vdesc[:5000],
                "merchant_deep_link": f"{SHOP}/products/{p['handle']}?variant={v['id']}",
                "merchant_image_url": img,
                "alternate_image": alt_imgs[0] if alt_imgs else "",
                "alternate_image_two": alt_imgs[1] if len(alt_imgs) > 1 else "",
                "search_price": f"{price:.2f}",
                "rrp_price": f"{rrp:.2f}" if rrp else "",
                "currency": CURRENCY,
                "in_stock": "1" if v.get("available") else "0",
                "brand_name": brand,
                "mpn": v.get("sku") or "",
                "ean": "",  # barcodes are not in products.json; NATKINA has none -> brand + mpn identify the item
                "merchant_category": ptype or "Jewellery",
                "google_product_category": gcat,
                "material": attrs["material"],
                "colour": attrs["colour"],
                "size": attrs["size"],
                "condition": "new",
                "product_type_path": " > ".join(x for x in ("Jewellery", ptype) if x),
            })
    if len({(x or "").strip().upper() for x in vendors}) < len(vendors):
        issues["vendor_spelling"].append(", ".join(sorted(str(x) for x in vendors)))
    for title, handles in names.items():
        if len(handles) > 1:
            issues["duplicate_titles"].append(f"{title}: {', '.join(handles)}")
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)


def write_google_xml(rows, path):
    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>\n')
    buf.write(f"<title>{BRAND}</title><link>{SHOP}</link><description>{BRAND} product feed</description>\n")
    for r in rows:
        avail = "in_stock" if r["in_stock"] == "1" else "out_of_stock"
        if r["rrp_price"]:
            price = f"<g:price>{r['rrp_price']} {r['currency']}</g:price><g:sale_price>{r['search_price']} {r['currency']}</g:sale_price>"
        else:
            price = f"<g:price>{r['search_price']} {r['currency']}</g:price>"
        extra = "".join(f"<g:additional_image_link>{escape(u)}</g:additional_image_link>"
                        for u in (r["alternate_image"], r["alternate_image_two"]) if u)
        attrs = "".join(f"<g:{k}>{escape(r[src])}</g:{k}>" for k, src in
                        (("material", "material"), ("color", "colour"), ("size", "size")) if r[src])
        buf.write(
            "<item>"
            f"<g:id>{r['product_id']}</g:id><g:item_group_id>{r['parent_product_id']}</g:item_group_id>"
            f"<g:title>{escape(r['product_name'])}</g:title><g:description>{escape(r['description'])}</g:description>"
            f"<g:link>{escape(r['merchant_deep_link'])}</g:link><g:image_link>{escape(r['merchant_image_url'])}</g:image_link>{extra}"
            f"<g:availability>{avail}</g:availability>{price}"
            f"<g:brand>{escape(r['brand_name'])}</g:brand><g:mpn>{escape(r['mpn'])}</g:mpn><g:identifier_exists>{'yes' if r['mpn'] else 'no'}</g:identifier_exists>"
            f"<g:condition>new</g:condition><g:google_product_category>{r['google_product_category']}</g:google_product_category>"
            f"<g:product_type>{escape(r['product_type_path'])}</g:product_type>{attrs}"
            "</item>\n")
    buf.write("</channel></rss>\n")
    path.write_text(buf.getvalue(), encoding="utf-8")


def main():
    if "--input" in sys.argv:
        products = json.loads(Path(sys.argv[sys.argv.index("--input") + 1]).read_text(encoding="utf-8"))["products"]
    else:
        products = fetch_all()
    issues = {k: [] for k in ("excluded", "empty_description", "empty_product_type", "no_image",
                              "empty_sku", "suspicious_size", "vendor_spelling",
                              "copy_handles", "duplicate_titles")}
    rows = build_rows(products, issues)
    if not rows:
        sys.exit("No rows built — feed NOT overwritten.")
    OUT.mkdir(exist_ok=True)
    write_csv(rows, OUT / "awin.csv")              # base feed, CHF
    write_google_xml(rows, OUT / "google.xml")
    market_lines = [f"CH  {CURRENCY}: awin.csv / google.xml — {len(rows)} variants"]
    if "--input" not in sys.argv:
        for country, cur in MARKETS.items():
            try:
                prices = fetch_market_prices(country)
                mrows, missing = localize(rows, prices, cur)
                got = {r["currency"] for r in mrows}
                if not mrows:
                    raise RuntimeError("no prices returned")
                if got != {cur}:
                    market_lines.append(f"{country} WARNING: expected {cur}, Shopify returned {sorted(got)}")
                tag = cur if got == {cur} else f"{country}_{'_'.join(sorted(got))}"
                write_csv(mrows, OUT / f"awin_{tag}.csv")
                write_google_xml(mrows, OUT / f"google_{tag}.xml")
                market_lines.append(f"{country} {cur}: awin_{tag}.csv / google_{tag}.xml — {len(mrows)} variants"
                                    + (f", {missing} without market price (not sold there)" if missing else ""))
            except Exception as e:
                market_lines.append(f"{country} {cur}: FAILED — {e} (feed not written; set STOREFRONT_TOKEN)")

    n_in = sum(r["in_stock"] == "1" for r in rows)
    rep = [f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
           f"Products: {len(products)} | variants in feed: {len(rows)} | in stock: {n_in} | out of stock: {len(rows) - n_in}",
           "", "Feeds by currency:"] + ["  " + x for x in market_lines] + [""]
    labels = {
        "excluded": "EXCLUDED from the feed (gift cards, donations, price 0)",
        "empty_description": "Products with EMPTY description (feed uses a generated fallback; fix in Shopify)",
        "empty_product_type": "Products with empty/unclear product type (category guessed from handle; set it in Shopify)",
        "no_image": "Products WITHOUT image (left out of the feed)",
        "empty_sku": "Variants without SKU (no MPN)",
        "suspicious_size": "One-digit size inside a product that uses two-digit sizes (typo?)",
        "vendor_spelling": "Vendors in the shop (Natkina/NATKINA merged; other brands kept as they are)",
        "copy_handles": "Published products with 'copy' in the URL (duplicates? check, unpublish if not needed)",
        "duplicate_titles": "Same product title on several products (publishers will see duplicates)",
    }
    for k, label in labels.items():
        rep.append(f"{label}: {len(issues[k])}")
        rep += [f"  - {x}" for x in issues[k][:50]]
    (OUT / "report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep[:2] + market_lines))


if __name__ == "__main__":
    main()