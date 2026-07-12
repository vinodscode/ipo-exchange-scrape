# India IPO Scraper — NSE + BSE, serverless

Scrapes IPO / public-issue data from **NSE** (current, upcoming and past
issues, plus per-IPO issue information) and **BSE** (live and forthcoming
public issues, plus per-IPO detail), merges both exchanges into one common
JSON file, and serves it as a plain static file — no server to run.

How it stays serverless: **GitHub Actions** runs the scraper on a schedule
and commits the result to `data/ipos.json`. Your static host (GitHub Pages,
Cloudflare Pages, Vercel) just serves the repo. The bundled `index.html` is a
small viewer over the same JSON.

> Why not scrape from the browser? NSE and BSE block cross-origin requests
> (CORS) and NSE additionally requires a cookie handshake, so a purely
> client-side page on Pages/Vercel cannot call their APIs directly. Scraping
> in a scheduled job and publishing static JSON is the standard workaround —
> and it's faster and kinder to the exchanges, too.

## Layout

```
scraper/scrape.py            # the scraper (Python 3.10+, only dependency: requests)
data/ipos.json               # generated output (committed by the Action)
index.html                   # static viewer for data/ipos.json
.github/workflows/scrape.yml # schedule: daily 07:20 IST + manual trigger
SCHEMA.md                    # documentation of the common JSON format
```

## Run locally

```sh
pip install -r requirements.txt
python scraper/scrape.py --out data/ipos.json
```

Useful flags:

- `--skip-details` — only fetch the list endpoints (fast; no per-IPO detail).
- `--past-months N` — how many months of past/closed issues to include (default 3).
- `--no-raw` — omit the untouched per-exchange payloads (`raw`) for a smaller file.

## Deploy

### GitHub Pages (simplest)

1. Push this repo to GitHub.
2. Repo **Settings → Actions → General → Workflow permissions** → select
   **Read and write permissions** (the Action commits the JSON back).
3. Repo **Settings → Pages** → Source: **Deploy from a branch** → `main` / root.
4. Run the workflow once manually (**Actions → Scrape IPO data → Run
   workflow**), or wait for the schedule.

Your data is then at `https://<user>.github.io/<repo>/data/ipos.json` and the
viewer at `https://<user>.github.io/<repo>/`.

### Cloudflare Pages / Vercel

Connect the repo in their dashboard with **no build command** and output
directory `/` (Vercel: framework preset "Other"). Every time the Action
commits fresh JSON, they redeploy the static files automatically.

### CORS for your own frontend

GitHub Pages, Cloudflare Pages and Vercel all serve static files with
`Access-Control-Allow-Origin: *` by default (Pages) or make it trivial to add,
so any app of yours can `fetch()` the JSON directly.

## Failure behaviour

- Each exchange is scraped independently; if one fails the other's data is
  still written, with the failure recorded in `sources.{nse,bse}.error`.
- If a scrape yields nothing at all, the previous `data/ipos.json` is left
  untouched (no commit), so the published feed never goes blank.
- NSE occasionally blocks datacenter IPs (GitHub Actions runners included).
  The scraper warms up cookies like a browser and retries with backoff, which
  works most of the time; a blocked run simply keeps the last good data. If
  blocks become chronic, run the scraper from any machine of yours on cron
  and `git push` — the hosting story is unchanged.

## Respectful scraping

The scraper makes a handful of GET requests per run (one per list, one per
IPO detail), identifies itself with a normal browser profile, and runs once
a day. Please keep the schedule modest — this reads public data that the
exchanges publish for investors, and hammering their APIs helps no one.
