# Insider Radar

Tracks open-market stock purchases by corporate insiders, finds companies where
several insiders bought in the same short window, and ranks them by a
rules-based conviction score.

Everything here is free: SEC EDGAR is public domain, the code is stdlib-only
Python, and the hosting path below costs nothing for a public repo.

## Test it locally (2 minutes, no network needed)

```bash
python3 insider_cluster.py selftest --write-demo
python3 insider_cluster.py serve
```

Open <http://127.0.0.1:8000>. That's synthetic data so you can see the interface
working before spending an hour polling EDGAR.

## Run it on real filings

```bash
python3 insider_cluster.py backfill --days 30
python3 insider_cluster.py prices          # liquidity data for clustered tickers
python3 insider_cluster.py export
python3 insider_cluster.py serve
```

Set your SEC contact first: `export INSIDER_UA="Your Name you@example.com"`.
EDGAR rejects requests without one. CI reads it from the `INSIDER_UA` secret.

The first backfill is slow. SEC caps you at 10 requests/second and a weekday
carries roughly 500–1,500 Form 4 filings, so 30 days takes 1–3 hours. It's
resumable — already-fetched days are skipped, so Ctrl-C and restart freely.

## Deploy it free

The poller needs a machine with outbound internet; the dashboard is static
files. GitHub gives you both at no cost:

1. Create a **public** repo and push these files.
2. Settings → Secrets and variables → Actions → new secret `INSIDER_UA`,
   value `Your Name your@email.com`.
3. Settings → Pages → source: deploy from branch `main`, folder `/ (root)`.
4. Actions tab → "Update insider data" → Run workflow, to seed it now rather
   than waiting for the overnight cron.

Your dashboard lands at `https://<your-username>.github.io/<repo>/`.

The workflow re-polls each weekday morning and commits the refreshed
`data.json`. Public repos get unlimited Actions minutes, so the running cost is
zero.

Two things to watch. The repo must be public for free Actions minutes and Pages
— which means your `insider.db` is public too; that's fine, it's all
public-domain SEC data, but don't add anything private to it. And the SEC
occasionally throttles cloud IP ranges more aggressively than residential ones,
so if Actions runs start failing on 403s, raise `REQ_DELAY` in the script.

## What each command does

| Command | Purpose |
|---|---|
| `backfill --days N` | Pull N days of filings from EDGAR into SQLite |
| `prices` | Fetch price and volume for clustered tickers (liquidity gate) |
| `clusters` | Print ranked clusters to the terminal |
| `export` | Write `docs/data.json` for the dashboard |
| `serve` | Serve `docs/` on localhost |
| `selftest` | Offline parse + scoring check on synthetic filings |

## What the score is built from

Positives: number of distinct insiders buying; seniority of the most senior
buyer (CFO weighted above CEO above other officers above directors above 10%
owners); the largest proportional increase in any one insider's holding; how
tightly the buys cluster in time; total dollars committed; an activist 13D in
the same window; a buyer with no regular buying pattern.

Penalties: the buy was part of a pre-scheduled 10b5-1 plan (not a fresh
decision); a registration or offering document is on file (dilution pending); a
late-filing notice (NT 10-K / NT 10-Q); an 8-K reporting an auditor change
(item 4.01) or that prior financials can't be relied on (item 4.02); the stock
is too illiquid to trade (the heaviest penalty in the model); the buyers follow
a routine annual schedule.

Routine-versus-opportunistic follows Cohen, Malloy and Pomorski (2012): an
insider who has bought in the same calendar month across consecutive years is
telling you about their bonus cycle, not the business. The classifier labels the
*insider*, not the individual trade, which is what the paper does. It needs at
least three prior buys across two years before it will commit — otherwise it
returns "unknown" and adjusts nothing, so early runs on a thin backfill simply
won't use this signal.

The context signals are free — they come from the same daily index file the
Form 4s come from, so they cost no extra requests. The 8-K item codes are the
exception: those need the document itself, so the script only fetches 8-Ks for
issuers that already cluster, which is a few dozen requests rather than
thousands.

**The weights are hand-set judgement calls, not fitted to anything.** They rank
candidates for reading, in an order I think is sensible. They are not a
validated model, and a number out of 100 will feel more precise than it is.
Every score is expandable in the dashboard so you can see exactly which rules
fired and disagree with them.

## Known gaps

- **The price feed is untested.** Prices come from Stooq, which is free and
  needs no key, but the machine this was built on could not reach stooq.com, so
  that one function has never run against the live service. Parsing is
  defensive and failures degrade to "liquidity unknown" rather than crashing —
  but check your first `prices` run by hand. Everything else has been tested.
- **Routine classification needs history.** It stays dormant until you have a
  few years backfilled. On 30 days of data every insider reads as "unknown".
- **US only.** UK companies file RNS/PDMR notices, not Form 4. Different
  scraper entirely.
- **Amendments.** Form 4/A filings are skipped rather than superseding the
  original. Fine for screening, wrong for a backtest.
- **No backtest.** Nothing here has been tested against forward returns. Treat
  the output as a reading list, not a signal.

Not investment advice.
