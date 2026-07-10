#!/usr/bin/env python3
"""Scrape NSE + BSE IPO data into one common JSON feed (data/ipos.json).

NSE endpoints (need a cookie warm-up visit + browser headers, Akamai-fronted):
  /api/ipo-current-issue                       -> Current Issues tab
  /api/all-upcoming-issues?category=ipo        -> Upcoming Issues tab
  /api/public-past-issues?from_date=&to_date=  -> Past Issues tab (DD-MM-YYYY,
                                                  both params required for range)
  /api/ipo-detail?symbol=&series=              -> Issue Information page
                                                  (works for active AND past)

BSE endpoints (need Referer: https://www.bseindia.com/ + browser UA, no cookies):
  GetPublicIssue_par_updated/w?flag=1&status={L|F}&ir_flag=IPO  -> publicissue page
  GetMkt_ISSUE_BBS_IPO/w?IPO_NO=                -> IPO detail (raw fields)
  Pubissues_GetBkbldgCatdem_ng/w?IPO_NO=        -> category-wise subscription
  MoreCompanyN/w?Fromdt={YYYY}&flag=1&type=2    -> listed-IPO performance (past)

Failure isolation: each exchange is scraped independently; a failure is recorded
in sources.{nse,bse}.error and the other exchange's data is still written.
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NSE_BASE = "https://www.nseindia.com"
NSE_WARMUP_PAGE = NSE_BASE + "/market-data/all-upcoming-issues-ipo"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
REQUEST_GAP = 0.6          # polite delay between consecutive API calls
RETRIES = 3
TIMEOUT = 30
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist():
    """Issue dates are Indian-market dates; a UTC runner must not flip
    boundary-day statuses 5.5 hours early."""
    return datetime.now(IST).date()


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# HTTP clients
# --------------------------------------------------------------------------- #

class NseClient:
    """NSE requires cookies from a real page visit; re-warms on 401/403."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._warm(retry=True)

    def _warm(self, retry=False):
        for attempt in range(RETRIES if retry else 1):
            try:
                self.session.cookies.clear()
                self.session.get(
                    NSE_WARMUP_PAGE,
                    headers={"Accept": "text/html,application/xhtml+xml"},
                    timeout=TIMEOUT,
                )
                return
            except requests.RequestException:
                if attempt == (RETRIES if retry else 1) - 1:
                    raise
                time.sleep(2 ** (attempt + 1))

    def get_json(self, path, params=None, referer=NSE_WARMUP_PAGE):
        last_err = None
        for attempt in range(RETRIES):
            try:
                resp = self.session.get(
                    NSE_BASE + path,
                    params=params,
                    headers={"Accept": "application/json", "Referer": referer},
                    timeout=TIMEOUT,
                )
                if resp.status_code in (401, 403):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                # covers blocks served as 401/403 AND as 200-with-HTML
                # (Akamai challenge pages make resp.json() raise ValueError)
                last_err = e
                time.sleep(2 ** (attempt + 1))
                try:
                    self._warm()
                except requests.RequestException:
                    pass
        raise RuntimeError(f"NSE {path} failed after {RETRIES} tries: {last_err}")


def bse_get_json(path, params=None):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://www.bseindia.com/",
    }
    last_err = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(f"{BSE_API}/{path}", params=params,
                                headers=headers, timeout=TIMEOUT,
                                allow_redirects=False)
            # A redirect means BSE rejected the request (bad Referer / block)
            if 300 <= resp.status_code < 400:
                raise requests.HTTPError(f"redirected (HTTP {resp.status_code})")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"BSE {path} failed after {RETRIES} tries: {last_err}")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y",
                "%Y-%m-%d", "%d/%m/%Y")


def parse_date(value):
    """'03-Jul-2026' / '30-JUN-2026' / '03-July-2026' / '2026-07-08T00:00:00'
    / '08 Jul 2026' -> 'YYYY-MM-DD' (or None)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    # split on 'T' only for ISO timestamps — all-caps months (30-OCT-2026)
    # contain a capital T of their own
    if re.match(r"^\d{4}-\d{2}-\d{2}T", text):
        text = text.split("T")[0]
    text = text.title()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_number(value):
    """'Rs.398', '₹1,23,456.78', '61.83', 158 -> float (or None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"(rs\.?|₹|,|\s)", "", str(value), flags=re.I)
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def parse_int(value):
    n = parse_number(value)
    return int(n) if n is not None else None


def parse_price_band(value):
    """'Rs.398 to Rs.419' / '398.00 - 419.00' / '398.00-419.00' /
    'Rs.94 to Rs.99 per equity share' / '158' -> {min, max, currency}."""
    if value is None:
        return None
    nums = [float(x) for x in
            re.findall(r"\d+(?:\.\d+)?", str(value).replace(",", ""))]
    if not nums:
        return None
    lo, hi = (nums[0], nums[1]) if len(nums) >= 2 else (nums[0], nums[0])
    return {"min": min(lo, hi), "max": max(lo, hi), "currency": "INR"}


def parse_period(value):
    """'03-July-2026 to 07-July-2026' -> (start, end) ISO dates."""
    if not value:
        return None, None
    parts = re.split(r"\s+to\s+", str(value), flags=re.I)
    start = parse_date(parts[0]) if parts else None
    end = parse_date(parts[1]) if len(parts) > 1 else None
    return start, end


_STOP_WORDS = {"limited", "ltd", "private", "pvt", "ltd.", "pvt."}


def company_key(name):
    """Normalised merge key: lowercase, no punctuation, no Ltd/Pvt suffixes."""
    words = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP_WORDS)


def slugify(name):
    return re.sub(r"\s+", "-", company_key(name)).strip("-") or "unknown"


def split_packed_entities(value):
    """BSE packs multi-entity strings: '#' between entities, '^' separates a
    name from its address, '|' separates address sub-fields."""
    if not value:
        return []
    return [part.split("^")[0].strip()
            for part in str(value).split("#") if part.split("^")[0].strip()]


CATEGORY_MAP = (
    ("qualified institutional", "qib"),
    ("non institutional", "nii"),
    ("non-institutional", "nii"),
    ("retail", "retail"),
    ("individual", "retail"),
    ("employee", "employee"),
    ("shareholder", "shareholders"),
    ("market maker", "marketMaker"),
)


def category_slot(label):
    text = (label or "").lower()
    for needle, slot in CATEGORY_MAP:
        if needle in text:
            return slot
    return None


def blank_record():
    return {
        "id": None,
        "companyName": None,
        "exchanges": [],
        "symbol": None,
        "bseScripCode": None,
        "series": None,
        "status": None,
        "issueType": None,
        "issueStartDate": None,
        "issueEndDate": None,
        "listingDate": None,
        "priceBand": None,
        "issuePrice": None,
        "faceValue": None,
        "lotSize": None,
        "issueSize": {"shares": None, "amount": None},
        "leadManagers": [],
        "registrar": None,
        "subscription": None,
        "urls": {"nse": None, "bse": None},
        "raw": {"nse": {}, "bse": {}},
    }


def fill(record, **values):
    """Set fields only when currently empty, so NSE data isn't clobbered by BSE
    (and vice versa) during merge."""
    for key, value in values.items():
        if value in (None, [], {}, ""):
            continue
        if record.get(key) in (None, [], {}, ""):
            record[key] = value
    return record


# --------------------------------------------------------------------------- #
# NSE
# --------------------------------------------------------------------------- #

# Keys of /api/ipo-detail that carry the price-level demand-graph blobs; they
# dwarf everything else, so they're excluded from `raw`.
NSE_DETAIL_SKIP = {"demandGraph", "demandGraphALL", "demandDataNSE",
                   "demandDataBSE"}


def nse_issue_info_map(detail):
    """issueInfo.dataList is ordered {title, value} rows; index by title."""
    rows = ((detail or {}).get("issueInfo") or {}).get("dataList") or []
    return {(row.get("title") or "").strip().lower(): (row.get("value") or "").strip()
            for row in rows if row.get("title")}


def nse_subscription(detail, list_row):
    """Combine the list row's headline numbers with the detail's category table."""
    offered = parse_int((list_row or {}).get("noOfSharesOffered"))
    bid = parse_int((list_row or {}).get("noOfsharesBid"))
    times = parse_number((list_row or {}).get("noOfTime"))

    categories = {}
    rows = ((detail or {}).get("activeCat") or {}).get("dataList") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("category") or ""
        if label.strip().lower() in ("category", "total"):
            if label.strip().lower() == "total" and times is None:
                times = parse_number(row.get("noOfTotalMeant"))
            continue
        slot = category_slot(label)
        value = parse_number(row.get("noOfTotalMeant"))
        if slot and value is not None and slot not in categories:
            categories[slot] = value

    if offered is None and bid is None and times is None and not categories:
        return None
    return {"timesSubscribed": times, "sharesOffered": offered,
            "sharesBid": bid, "categories": categories or None}


def nse_apply_detail(record, detail):
    info = nse_issue_info_map(detail)
    for title, value in info.items():
        if "issue period" in title:
            start, end = parse_period(value)
            fill(record, issueStartDate=start, issueEndDate=end)
        elif "price range" in title or "price band" in title:
            fill(record, priceBand=parse_price_band(value))
        elif "lot size" in title or "bid lot" in title:
            fill(record, lotSize=parse_int(value))
        elif "face value" in title:
            fill(record, faceValue=parse_number(value))
        elif "issue type" in title:
            fill(record, issueType=value)
        elif "issue size" in title:
            # e.g. "... fresh Issue up to 48,39,600 Equity Shares ..."; only
            # trust a lone share count — multi-number texts (fresh + OFS +
            # totals) are ambiguous and stay available in raw.
            counts = re.findall(r"([\d,]{4,})\s+(?:equity\s+)?shares", value,
                                flags=re.I)
            if len(counts) == 1 and not record["issueSize"]["shares"]:
                record["issueSize"]["shares"] = parse_int(counts[0])
        elif "lead manager" in title:
            # NSE wraps the value in literal quotes and separates managers
            # with both commas and "and".
            cleaned = value.strip().strip('"').strip()
            managers = [m.strip() for m in
                        re.split(r"\s*,\s*|\s+and\s+", cleaned) if m.strip()]
            fill(record, leadManagers=managers)
        elif "registrar" in title and "address" not in title:
            fill(record, registrar=value)
        elif "listing" in title and "date" in title:
            fill(record, listingDate=parse_date(value))

    meta = (detail or {}).get("metaInfo") or {}
    if isinstance(meta, dict):
        fill(record, listingDate=parse_date(meta.get("listingDate")))


def fetch_nse(past_months, with_details, include_raw):
    """Returns (records, errors). Partial failures collect into `errors`
    while every bucket that worked is still returned."""
    client = NseClient()
    records, errors = [], []

    buckets = []
    try:
        buckets.append(("open", client.get_json("/api/ipo-current-issue") or []))
    except Exception as e:
        errors.append(f"current: {e}")
    time.sleep(REQUEST_GAP)
    try:
        buckets.append(("upcoming",
                        client.get_json("/api/all-upcoming-issues",
                                        {"category": "ipo"}) or []))
    except Exception as e:
        errors.append(f"upcoming: {e}")
    if past_months > 0:
        time.sleep(REQUEST_GAP)
        try:
            today = today_ist()
            frm = today - timedelta(days=past_months * 31)
            past = client.get_json("/api/public-past-issues", {
                "from_date": frm.strftime("%d-%m-%Y"),
                "to_date": today.strftime("%d-%m-%Y"),
            }) or []
            buckets.append(("closed", past))
        except Exception as e:
            errors.append(f"past: {e}")

    # Dedupe across ALL buckets: NSE's past feed repeats rows verbatim, and on
    # boundary days the same issue can appear in two tabs. First bucket wins
    # (open > upcoming > closed), keeping ids unique downstream.
    seen = set()
    for status, rows in buckets:
        unique_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("symbol") or row.get("company")
                   or row.get("companyName"),
                   row.get("issueStartDate") or row.get("ipoStartDate"))
            if key in seen:
                continue
            seen.add(key)
            unique_rows.append(row)

        for row in unique_rows:
            record = blank_record()
            name = row.get("companyName") or row.get("company")
            symbol = row.get("symbol")
            series = row.get("series") or row.get("securityType")
            series = {"BE": "EQ", "EQUITY": "EQ"}.get(
                (series or "").upper(), (series or "").upper()) or None
            start = parse_date(row.get("issueStartDate") or row.get("ipoStartDate"))
            end = parse_date(row.get("issueEndDate") or row.get("ipoEndDate"))
            fill(record,
                 companyName=name,
                 symbol=symbol,
                 series=series,
                 status=status,
                 issueStartDate=start,
                 issueEndDate=end,
                 listingDate=parse_date(row.get("listingDate")),
                 priceBand=parse_price_band(row.get("issuePrice")
                                            if status == "upcoming"
                                            else row.get("priceRange")),
                 issuePrice=parse_number(row.get("issuePrice"))
                            if status == "closed" else None,
                 subscription=nse_subscription(None, row)
                              if status == "open" else None)
            if status == "upcoming":
                shares = parse_int(row.get("issueSize"))
                if shares:
                    record["issueSize"]["shares"] = shares
            if symbol and series:
                record["urls"]["nse"] = (
                    f"{NSE_BASE}/market-data/issue-information"
                    f"?symbol={symbol}&series={series}"
                    f"&type={'Active' if status == 'open' else 'Past' if status == 'closed' else 'Upcoming'}"
                )
            if include_raw:
                record["raw"]["nse"]["list"] = row

            if with_details and symbol:
                time.sleep(REQUEST_GAP)
                try:
                    detail = client.get_json(
                        "/api/ipo-detail",
                        {"symbol": symbol, "series": series or "EQ"},
                        referer=f"{NSE_BASE}/market-data/issue-information"
                                f"?symbol={symbol}&series={series or 'EQ'}&type=Active",
                    )
                    nse_apply_detail(record, detail)
                    # deliberately overwrite: the detail-based subscription is
                    # a superset of the list-row one set at record creation
                    subscription = nse_subscription(
                        detail, row if status == "open" else None)
                    if subscription:
                        record["subscription"] = subscription
                    if include_raw and isinstance(detail, dict):
                        record["raw"]["nse"]["detail"] = {
                            k: v for k, v in detail.items()
                            if k not in NSE_DETAIL_SKIP}
                except Exception as e:
                    log(f"  NSE detail failed for {symbol}: {e}")
            records.append(record)
        log(f"NSE {status}: {len(unique_rows)} issues")

    return records, errors


# --------------------------------------------------------------------------- #
# BSE
# --------------------------------------------------------------------------- #

def bse_subscription(sub_payload):
    rows = (sub_payload or {}).get("table2") or []
    times = None
    categories = {}
    for row in rows:
        label = (row.get("col2") or "").strip()
        if label.lower() in ("category", ""):
            continue
        value = parse_number(row.get("col5"))
        if label.lower() == "total":
            times = value
            continue
        slot = category_slot(label)
        if slot and value is not None and slot not in categories:
            categories[slot] = value
    if times is None and not categories:
        return None
    return {"timesSubscribed": times, "sharesOffered": None,
            "sharesBid": None, "categories": categories or None}


def bse_apply_detail(record, detail, sub_payload):
    rows = (detail or {}).get("IPONO_0") or []
    head = rows[0] if rows and isinstance(rows[0], dict) else {}
    start, end = parse_period(head.get("Issue_Period"))
    fill(record,
         symbol=str(head.get("Symbol") or "").strip() or None,
         issueStartDate=start,
         issueEndDate=end,
         priceBand=parse_price_band(head.get("Price_Band")),
         faceValue=parse_number(head.get("Face_Value")),
         lotSize=parse_int(head.get("Market_Lot")),
         issueType=(head.get("Security_Type") or "").strip() or None,
         registrar=next(iter(split_packed_entities(head.get("Registrar"))), None),
         leadManagers=split_packed_entities(head.get("Book_Running_Lead_Manager")))
    shares = parse_int(head.get("Issue_Size_No_of_shares"))
    if shares and not record["issueSize"]["shares"]:
        record["issueSize"]["shares"] = shares
    fill(record, subscription=bse_subscription(sub_payload))


def fetch_bse(past_months, with_details, include_raw):
    records, errors = [], []

    rows = []
    try:
        payload = bse_get_json("GetPublicIssue_par_updated/w",
                               {"flag": 1, "status": "", "exchange": "",
                                "ir_flag": "IPO"})
        rows = (payload or {}).get("Table") or []
        log(f"BSE live+forthcoming: {len(rows)} IPOs")
    except Exception as e:
        errors.append(f"list: {e}")

    for row in rows:
        if not isinstance(row, dict):
            continue
        record = blank_record()
        platform = (row.get("eXCHANGE_PLATFORM") or "").lower()
        fill(record,
             companyName=row.get("Scrip_Name"),
             bseScripCode=str(row.get("Scrip_cd")) if row.get("Scrip_cd") else None,
             series="SME" if "sme" in platform else "EQ" if platform else None,
             status="open" if row.get("Status") == "L" else "upcoming",
             issueStartDate=parse_date(row.get("Start_Dt")),
             issueEndDate=parse_date(row.get("End_Dt")),
             priceBand=parse_price_band(row.get("Price_Band")),
             faceValue=parse_number(row.get("Face_Val")))
        record["urls"]["bse"] = "https://www.bseindia.com/publicissue"
        if include_raw:
            record["raw"]["bse"]["list"] = row

        ipo_no = row.get("IPO_NO")
        if with_details and ipo_no:
            time.sleep(REQUEST_GAP)
            detail, sub_payload = None, None
            try:
                detail = bse_get_json("GetMkt_ISSUE_BBS_IPO/w", {"IPO_NO": ipo_no})
            except Exception as e:
                log(f"  BSE detail failed for IPO_NO={ipo_no}: {e}")
            time.sleep(REQUEST_GAP)
            try:
                sub_payload = bse_get_json("Pubissues_GetBkbldgCatdem_ng/w",
                                           {"IPO_NO": ipo_no})
            except Exception as e:
                log(f"  BSE subscription failed for IPO_NO={ipo_no}: {e}")
            if detail or sub_payload:
                try:
                    bse_apply_detail(record, detail, sub_payload)
                except Exception as e:
                    log(f"  BSE detail parse failed for IPO_NO={ipo_no}: {e}")
                if include_raw:
                    if detail:
                        record["raw"]["bse"]["detail"] = detail
                    if sub_payload:
                        record["raw"]["bse"]["subscription"] = sub_payload
        records.append(record)

    if past_months > 0:
        today = today_ist()
        window_start = today - timedelta(days=past_months * 31)
        for year in range(window_start.year, today.year + 1):
            time.sleep(REQUEST_GAP)
            try:
                payload = bse_get_json("MoreCompanyN/w",
                                       {"Fromdt": year, "company": "",
                                        "flag": 1, "type": 2})
                tracker_rows = (payload or {}).get("Table") or []
            except Exception as e:
                errors.append(f"past {year}: {e}")
                continue
            kept = 0
            for row in tracker_rows:
                if not isinstance(row, dict):
                    continue
                try:
                    listed = parse_date(row.get("ListedOn"))
                    if not listed or listed < window_start.isoformat():
                        continue
                    record = blank_record()
                    fill(record,
                         companyName=row.get("CompanyName"),
                         status="closed",
                         listingDate=listed,
                         issuePrice=parse_number(row.get("IssuePrice")))
                    record["urls"]["bse"] = row.get("IMAGE") or None
                    if include_raw:
                        record["raw"]["bse"]["list"] = row
                    records.append(record)
                    kept += 1
                except Exception as e:
                    log(f"  BSE tracker row skipped ({year}): {e}")
            log(f"BSE past {year}: kept {kept} of {len(tracker_rows)} listed IPOs")

    return records, errors


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

STATUS_ORDER = {"open": 0, "upcoming": 1, "closed": 2}


def resolve_status(record):
    """Dates are authoritative when present; exchange-declared status otherwise."""
    today = today_ist().isoformat()
    start, end = record["issueStartDate"], record["issueEndDate"]
    if start and end:
        if start <= today <= end:
            return "open"
        if today < start:
            return "upcoming"
        return "closed"
    return record["status"]


def merge_records(nse_records, bse_records):
    merged = list(nse_records)
    for rec in merged:
        rec["exchanges"] = ["NSE"]

    # both indexes must agree on which record wins a collision: the first
    by_symbol = {}
    by_key = {}
    for rec in merged:
        if rec["symbol"]:
            by_symbol.setdefault(rec["symbol"], rec)
        by_key.setdefault(company_key(rec["companyName"]), rec)

    for bse in bse_records:
        target = None
        if bse["symbol"] and bse["symbol"] in by_symbol:
            target = by_symbol[bse["symbol"]]
        else:
            target = by_key.get(company_key(bse["companyName"]))

        if target is None:
            bse["exchanges"] = ["BSE"]
            merged.append(bse)
            key = company_key(bse["companyName"])
            by_key.setdefault(key, bse)
            if bse["symbol"]:
                by_symbol.setdefault(bse["symbol"], bse)
            continue

        if "BSE" not in target["exchanges"]:
            target["exchanges"].append("BSE")
        fill(target,
             symbol=bse["symbol"],
             bseScripCode=bse["bseScripCode"],
             series=bse["series"],
             issueType=bse["issueType"],
             issueStartDate=bse["issueStartDate"],
             issueEndDate=bse["issueEndDate"],
             listingDate=bse["listingDate"],
             priceBand=bse["priceBand"],
             issuePrice=bse["issuePrice"],
             faceValue=bse["faceValue"],
             lotSize=bse["lotSize"],
             leadManagers=bse["leadManagers"],
             registrar=bse["registrar"],
             subscription=bse["subscription"])
        if bse["issueSize"]["shares"] and not target["issueSize"]["shares"]:
            target["issueSize"]["shares"] = bse["issueSize"]["shares"]
        # first BSE match wins — a later tracker row must not clobber the
        # richer live-list detail already merged in
        if not target["urls"]["bse"]:
            target["urls"]["bse"] = bse["urls"]["bse"]
        if not target["raw"]["bse"]:
            target["raw"]["bse"] = bse["raw"]["bse"]

    for rec in merged:
        rec["status"] = resolve_status(rec)
        rec["id"] = slugify(rec["companyName"])

    # stable multi-pass sort: newest first within each status bucket
    merged.sort(key=lambda r: r["companyName"] or "")
    merged.sort(key=lambda r: r["issueStartDate"] or r["listingDate"] or "",
                reverse=True)
    merged.sort(key=lambda r: STATUS_ORDER.get(r["status"], 3))
    return merged


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Scrape NSE+BSE IPO data")
    parser.add_argument("--out", default="data/ipos.json")
    parser.add_argument("--past-months", type=int, default=3,
                        help="months of past/closed issues to include (0=none)")
    parser.add_argument("--skip-details", action="store_true",
                        help="only fetch list endpoints, no per-IPO detail")
    parser.add_argument("--no-raw", action="store_true",
                        help="omit untouched exchange payloads from output")
    args = parser.parse_args()
    with_details = not args.skip_details
    include_raw = not args.no_raw

    sources = {}
    nse_records, bse_records = [], []

    log("Scraping NSE...")
    try:
        nse_records, nse_errors = fetch_nse(args.past_months, with_details,
                                            include_raw)
        sources["nse"] = {"ok": not nse_errors,
                          "error": "; ".join(nse_errors) or None}
    except Exception as e:
        sources["nse"] = {"ok": False, "error": str(e)}
        log(f"NSE failed entirely: {e}")

    log("Scraping BSE...")
    try:
        bse_records, bse_errors = fetch_bse(args.past_months, with_details,
                                            include_raw)
        sources["bse"] = {"ok": not bse_errors,
                          "error": "; ".join(bse_errors) or None}
    except Exception as e:
        sources["bse"] = {"ok": False, "error": str(e)}
        log(f"BSE failed entirely: {e}")

    ipos = merge_records(nse_records, bse_records)
    if not include_raw:
        for rec in ipos:
            rec.pop("raw", None)
    out_path = Path(args.out)

    if not ipos and out_path.exists():
        # Never blank out a previously good feed because of a bad scrape.
        log("Scrape produced 0 records; keeping existing output untouched.")
        sys.exit(1 if not (sources["nse"]["ok"] or sources["bse"]["ok"]) else 0)

    counts = {"open": 0, "upcoming": 0, "closed": 0}
    for rec in ipos:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")
                       .replace("+00:00", "Z"),
        "sources": sources,
        "counts": counts,
        "ipos": ipos,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=1))
    log(f"Wrote {len(ipos)} IPOs to {out_path} "
        f"(open={counts['open']}, upcoming={counts['upcoming']}, "
        f"closed={counts['closed']})")


if __name__ == "__main__":
    main()
