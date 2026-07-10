# Common IPO JSON schema

The scraper writes a single file, `data/ipos.json`, merging NSE and BSE into one
common format. IPOs listed on both exchanges are merged into a single record
(matched on normalised company name), with `exchanges` listing where it appears
and `raw` preserving each exchange's untouched payload.

```jsonc
{
  "generatedAt": "2026-07-07T09:30:00Z",   // UTC timestamp of this scrape
  "sources": {
    "nse": { "ok": true,  "error": null },  // per-exchange fetch health
    "bse": { "ok": true,  "error": null }
  },
  "counts": { "open": 2, "upcoming": 5, "closed": 40 },
  "ipos": [
    {
      "id": "ic-electricals-company",       // stable slug from company name
      "companyName": "IC Electricals Company Limited",
      "exchanges": ["NSE", "BSE"],          // where this issue appears
      "symbol": "ICELCO",                   // trading symbol (null when unknown)
      "bseScripCode": "544999",             // BSE scrip code (null for NSE-only)
      "series": "SME",                      // "EQ" (mainboard) | "SME" |
                                            // "RR" (REIT) | "IV" (InvIT) | null
      "status": "open",                     // "open" | "upcoming" | "closed"
      "issueType": "Book Building",         // as reported by the exchange
      "issueStartDate": "2026-07-03",       // ISO dates, null when unknown
      "issueEndDate": "2026-07-07",
      "listingDate": null,
      "priceBand": { "min": 100.0, "max": 105.0, "currency": "INR" },
      "issuePrice": 105.0,                  // final discovered price (closed issues)
      "faceValue": 10.0,
      "lotSize": 1200,
      "issueSize": {
        "shares": 3471600,                  // total shares offered, if known
        "amount": null                      // rupees, if reported
      },
      "leadManagers": ["..."],
      "registrar": "...",
      "subscription": {
        "timesSubscribed": 61.83,
        "sharesOffered": 3471600,
        "sharesBid": 214632000,
        "categories": {                     // category-wise, when available
          "qib": 10.5, "nii": 80.2, "retail": 55.1
        }
      },
      "urls": {                             // deep links to exchange pages
        "nse": "https://www.nseindia.com/market-data/issue-information?symbol=ICELCO&series=SME&type=Active",
        "bse": "https://www.bseindia.com/publicissue.html?id=..."
      },
      "raw": {                              // untouched exchange payloads —
        "nse": { "list": {}, "detail": {} },// everything not mapped above is
        "bse": { "list": {}, "detail": {} } // still available here
      }
    }
  ]
}
```

Conventions:

- Every field is present on every record; missing data is `null` (or `[]`),
  never absent, so consumers don't need existence checks.
- Dates are `YYYY-MM-DD`; the exchanges' `03-Jul-2026` / `03 Jul 2026` variants
  are normalised.
- Numbers are parsed (`"61.83"` → `61.83`); comma-grouped Indian-format
  numbers are handled.
- `status` is derived from the issue dates when both are present (they are
  authoritative), falling back to the exchange's own tab/flag.
- `raw.nse.detail` excludes NSE's `demandGraph*` / `demandData*` keys — the
  price-level demand-curve blobs dwarf everything else in the response. All
  other detail keys (`issueInfo`, `bidDetails`, `activeCat`, `metaInfo`) are
  kept verbatim. Pass `--no-raw` to drop `raw` entirely (≈10× smaller file).
