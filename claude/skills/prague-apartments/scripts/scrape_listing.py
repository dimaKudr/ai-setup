#!/usr/bin/env python3
"""
Scrape Czech real-estate listing pages (sreality.cz, bezrealitky.cz, or a
generic fallback for anything else) into a normalized JSON record per URL.

Usage:
    python3 scrape_listing.py <url> [<url> ...]
    python3 scrape_listing.py --file urls.txt

Output: JSON array on stdout, one object per URL, in input order.
Fields that couldn't be determined are null - fill them in manually before
running the analysis step.
"""
import sys
import re
import json
import argparse
import urllib.request
import urllib.error

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)

# A site's "garage: true" / "parking: true" boolean field only means a
# parking spot EXISTS in the building - it says nothing about whether it's
# bundled into the listed price or sold as a separate line item. Sellers
# often price it separately (a common Czech listing pattern is a sentence
# like "Parkovací místo v suterénu domu 750 000,-" at the end of the
# description). Always resolve inclusion from the free-text description,
# never from the boolean alone.
PARKING_SENTENCE_RE = re.compile(
    # "park" alone also matches a public park ("parku plného zeleně") -
    # anchor on parking-specific stems so we don't pick up unrelated
    # neighborhood-park sentences as "parking" evidence.
    r"[^.\n]*(?:parkovac|parkovišt|parking|gará)[^.\n]*", re.I
)
PARKING_INCLUDED_KW = re.compile(
    r"součást[íi]|v cen[ěe]|již v cen|included in (the )?price"
    r"|nálež[íi]|patř[íi]\s+(k|do)",  # "K jednotce náleží..." / "patří k bytu"
    re.I,
)
PARKING_EXCLUDED_KW = re.compile(
    r"není\s+součást|není\s+v\s+cen|nen[ií]\s+zahrnut|za\s+příplatek|za\s+doplatek|"
    r"nejsou\s+součást|v\s+cen[ěe]\s+není",
    re.I,
)
PARKING_PRICE_RE = re.compile(
    r"(\d[\d\s ]{4,9})\s*,?-?\s*Kč|Kč\s*(\d[\d\s ]{4,9})|(\d[\d\s ]{4,9})\s*,-"
)


def detect_parking_pricing(description: str):
    """Returns (garage_in_price, garage_extra_cost_czk, evidence) from the
    listing's free-text description. All three are None if no parking/garage
    is mentioned at all (so callers shouldn't assume "no parking" either -
    it may just not be described in prose)."""
    if not description:
        return None, None, None

    sentences = PARKING_SENTENCE_RE.findall(description)
    if not sentences:
        return None, None, None

    # Negated inclusion ("... není součástí ceny", "za příplatek") must win
    # over the inclusion keywords below, which would otherwise match the
    # "součástí"/"náleží" in the same sentence.
    for sentence in sentences:
        if PARKING_EXCLUDED_KW.search(sentence):
            price_m = PARKING_PRICE_RE.search(sentence)
            extra = None
            if price_m:
                digits = re.sub(r"[^\d]", "", next(g for g in price_m.groups() if g))
                extra = int(digits) if digits and int(digits) >= 50000 else None
            return False, extra, sentence.strip()

    for sentence in sentences:
        if PARKING_INCLUDED_KW.search(sentence):
            return True, None, sentence.strip()

    for sentence in sentences:
        price_m = PARKING_PRICE_RE.search(sentence)
        if price_m:
            digits = re.sub(r"[^\d]", "", next(g for g in price_m.groups() if g))
            if digits and int(digits) >= 50000:  # filter out area/floor numbers
                return False, int(digits), sentence.strip()

    # Parking/garage is mentioned but neither an inclusion phrase nor a
    # standalone price was found near it - genuinely ambiguous.
    return None, None, sentences[0].strip()


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_next_data(html: str):
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# sreality.cz
# ---------------------------------------------------------------------------

def parse_sreality(html: str, url: str) -> dict:
    record = _blank_record(url, "sreality")
    data = extract_next_data(html)
    if not data:
        record["error"] = "could not find __NEXT_DATA__ on sreality page"
        return record

    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        record["error"] = "unexpected sreality page structure"
        return record

    est = None
    for q in queries:
        key = q.get("queryKey")
        if isinstance(key, list) and key and key[0] == "estate":
            est = q.get("state", {}).get("data")
            break

    if not est:
        record["error"] = "estate data not found (listing may be removed/expired)"
        return record

    params = est.get("params") or {}
    locality = est.get("locality") or {}

    record["title"] = est.get("name")
    record["district"] = locality.get("cityPart") or locality.get("district")
    record["price_czk"] = est.get("priceCzk") or est.get("priceSummaryCzk")
    record["area_m2"] = params.get("usableArea") or params.get("floorArea")
    record["price_per_m2"] = est.get("priceCzkPerSqM")

    furnished = (params.get("furnished") or {}).get("name")
    record["furnished"] = furnished
    record["has_kitchen_or_furniture"] = _furnished_to_bool(furnished)

    condition = (params.get("buildingCondition") or {}).get("name")
    record["building_condition"] = condition
    record["ready_date"] = params.get("readyDate") or params.get("finishDate")

    record["garage"] = bool(params.get("garage"))
    garage_in_price, garage_extra_cost, evidence = detect_parking_pricing(
        est.get("description") or ""
    )
    record["garage_in_price"] = garage_in_price
    record["garage_extra_cost_czk"] = garage_extra_cost
    record["garage_evidence"] = evidence
    record["balcony"] = bool(params.get("balcony"))
    record["cellar"] = bool(params.get("cellar"))
    record["elevator"] = bool((params.get("elevator") or {}).get("value"))
    record["ownership"] = (params.get("ownership") or {}).get("name")
    record["reserved"] = "rezervov" in (est.get("note") or "").lower()

    return record


def _furnished_to_bool(name):
    """sreality 'furnished' field -> does listing include kitchen/furniture."""
    if not name:
        return None
    name = name.lower()
    if "ne" == name.strip():
        return False
    if "ano" in name or "část" in name:  # Ano / Částečně
        return True
    return None


# ---------------------------------------------------------------------------
# bezrealitky.cz
# ---------------------------------------------------------------------------

def parse_bezrealitky(html: str, url: str) -> dict:
    record = _blank_record(url, "bezrealitky")
    data = extract_next_data(html)
    if not data:
        record["error"] = "could not find __NEXT_DATA__ on bezrealitky page"
        return record

    try:
        adv = data["props"]["pageProps"]["origAdvert"]
    except (KeyError, TypeError):
        record["error"] = "unexpected bezrealitky page structure"
        return record

    if not adv:
        record["error"] = "advert data not found (listing may be removed/expired)"
        return record

    record["title"] = adv.get("disposition")
    address = adv.get("address") or ""
    # address like "Vršovická, Praha - Vršovice"
    district = None
    if " - " in address:
        district = address.split(" - ")[-1].strip()
    record["district"] = district or adv.get("city")

    record["price_czk"] = adv.get("price")
    record["area_m2"] = adv.get("surface")
    if record["price_czk"] and record["area_m2"]:
        record["price_per_m2"] = round(record["price_czk"] / record["area_m2"])

    equipped = adv.get("equipped")
    record["furnished"] = equipped
    if equipped:
        record["has_kitchen_or_furniture"] = equipped.upper() != "NOT_EQUIPPED"

    record["building_condition"] = adv.get("condition")
    record["garage"] = bool(adv.get("garage"))
    garage_in_price, garage_extra_cost, evidence = detect_parking_pricing(
        adv.get("description") or ""
    )
    record["garage_in_price"] = garage_in_price
    record["garage_extra_cost_czk"] = garage_extra_cost
    record["garage_evidence"] = evidence
    record["balcony"] = bool(adv.get("balcony"))
    record["cellar"] = bool(adv.get("cellar"))
    record["elevator"] = bool(adv.get("lift"))
    record["ownership"] = adv.get("ownership")
    record["reserved"] = bool(adv.get("reserved"))

    return record


# ---------------------------------------------------------------------------
# central-group.cz (new-build developer, Vue SPA - use its JSON API)
# ---------------------------------------------------------------------------

CG_CATALOG_RE = re.compile(r"/byt-detail/([\w-]+)")


def parse_central_group(url: str) -> dict:
    record = _blank_record(url, "central-group")
    m = CG_CATALOG_RE.search(url)
    if not m:
        record["error"] = "could not parse catalogNumber from central-group.cz URL"
        return record
    catalog_number = m.group(1)

    try:
        req = urllib.request.Request(
            "https://www.central-group.cz/api/system/time-version", headers=HEADERS
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            time_id = resp.read().decode("utf-8").strip()

        api_url = (
            f"https://www.central-group.cz/api/apartment/{catalog_number}"
            f"?excludeFakeSold=true&timeId={time_id}&langId=1"
        )
        req = urllib.request.Request(api_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            apt = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError) as e:
        record["error"] = f"central-group API failed: {e}"
        return record

    location = apt.get("location") or {}
    record["title"] = f"{apt.get('layoutLabel')} {apt.get('innerFloorArea')} m² ({location.get('name')})"
    record["district"] = location.get("cityPart")
    record["price_czk"] = apt.get("totalPriceWithVAT")
    record["area_m2"] = apt.get("innerFloorArea")
    if record["price_czk"] and record["area_m2"]:
        record["price_per_m2"] = round(record["price_czk"] / record["area_m2"])

    # New-build developer sales are shell/unfurnished unless a kitchen
    # cabinet is explicitly included.
    record["has_kitchen_or_furniture"] = bool(apt.get("hasKitchenCabinet"))
    record["furnished"] = "kitchen cabinet included" if apt.get("hasKitchenCabinet") else "shell (no kitchen)"

    record["building_condition"] = "Novostavba (pre-sale)"
    record["ready_date"] = apt.get("completionDate")

    # central-group.cz lists parking spaces and storage units as separate
    # sellable items, each carrying its OWN totalPriceWithVAT distinct from
    # the apartment's price - that means they are priced add-ons, not
    # bundled into the apartment price, unless proven otherwise. Surface the
    # per-item price rather than assuming inclusion just because the array
    # is non-empty.
    parking_places = apt.get("parkingPlaces") or []
    record["garage"] = bool(parking_places)
    record["garage_in_price"] = False if parking_places else None
    record["garage_extra_cost_czk"] = (
        parking_places[0].get("totalPriceWithVAT") if parking_places else None
    )

    storage = apt.get("storageFacilities") or []
    record["cellar"] = bool(storage)
    record["cellar_in_price"] = False if storage else None
    record["cellar_extra_cost_czk"] = (
        storage[0].get("totalPriceWithVAT") if storage else None
    )

    record["balcony"] = bool(apt.get("balconies")) or bool(apt.get("terraces"))
    record["elevator"] = None  # not exposed by this API
    record["ownership"] = "Osobní"
    record["reserved"] = bool(apt.get("sold")) or apt.get("saleStatus") not in (0, None)

    return record


# ---------------------------------------------------------------------------
# generic fallback (idnes reality, propexa.cz, agency sites, etc.)
# ---------------------------------------------------------------------------

PRICE_RE = re.compile(r"([\d\s ]{6,})\s*Kč")
AREA_RE = re.compile(r"(\d{2,4})\s*m\s*2|(\d{2,4})\s*m²")
OG_DESC_RE = re.compile(
    r'<meta property="og:description" content="([^"]*)"', re.S
)
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"', re.S)


def parse_generic(html: str, url: str) -> dict:
    record = _blank_record(url, "generic")
    record["_note"] = "generic fallback used - verify all fields manually"

    title_m = OG_TITLE_RE.search(html)
    if title_m:
        record["title"] = title_m.group(1)

    desc_m = OG_DESC_RE.search(html)
    text = desc_m.group(1) if desc_m else html

    price_m = PRICE_RE.search(text) or PRICE_RE.search(html)
    if price_m:
        digits = re.sub(r"[^\d]", "", price_m.group(1))
        if digits:
            record["price_czk"] = int(digits)

    area_m = AREA_RE.search(text) or AREA_RE.search(html)
    if area_m:
        val = area_m.group(1) or area_m.group(2)
        if val:
            record["area_m2"] = int(val)

    if record["price_czk"] and record["area_m2"]:
        record["price_per_m2"] = round(record["price_czk"] / record["area_m2"])

    kitchen_kw = ["kuchy", "vybaven", "zařízen"]
    record["has_kitchen_or_furniture"] = (
        any(kw in html.lower() for kw in kitchen_kw) or None
    )

    return record


# ---------------------------------------------------------------------------

def _blank_record(url: str, source: str) -> dict:
    return {
        "url": url,
        "source": source,
        "title": None,
        "district": None,
        "price_czk": None,
        "area_m2": None,
        "price_per_m2": None,
        "furnished": None,
        "has_kitchen_or_furniture": None,
        "building_condition": None,
        "ready_date": None,
        "garage": None,
        "garage_in_price": None,
        "garage_extra_cost_czk": None,
        "garage_evidence": None,
        "balcony": None,
        "cellar": None,
        "elevator": None,
        "ownership": None,
        "reserved": None,
        "error": None,
    }


def scrape_one(url: str) -> dict:
    if "central-group.cz" in url:
        return parse_central_group(url)

    try:
        html = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        record = _blank_record(url, "unknown")
        record["error"] = f"fetch failed: {e}"
        return record

    if "sreality.cz" in url:
        return parse_sreality(html, url)
    if "bezrealitky.cz" in url:
        return parse_bezrealitky(html, url)
    return parse_generic(html, url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", help="listing URLs")
    ap.add_argument("--file", help="file with one URL per line")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file) as f:
            urls += [line.strip() for line in f if line.strip()]

    if not urls:
        print("no URLs given", file=sys.stderr)
        sys.exit(1)

    results = [scrape_one(u) for u in urls]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
