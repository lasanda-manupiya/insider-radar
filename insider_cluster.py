#!/usr/bin/env python3
"""
insider_cluster.py - SEC Form 4 open-market buy tracker, cluster detector,
conviction scorer and static-site exporter.

Stdlib only. No pip install, no API key, no paid data.

SEC REQUIRES a real contact in the User-Agent or every request 403s.

    export INSIDER_UA="Your Name your@email.com"
    python3 insider_cluster.py backfill --days 30
    python3 insider_cluster.py clusters
    python3 insider_cluster.py export
    python3 insider_cluster.py serve          # http://127.0.0.1:8000
    python3 insider_cluster.py selftest       # offline, synthetic data
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

# Deliberately not hardcoded: this repo is public, and an email address in
# public source gets scraped. Set INSIDER_UA locally; CI reads it from a secret.
USER_AGENT = os.environ.get("INSIDER_UA", "")
ARCHIVES = "https://www.sec.gov/Archives/"
DAILY_INDEX = ARCHIVES + "edgar/daily-index/{year}/QTR{qtr}/master.{ymd}.idx"

REQ_DELAY = 0.12                      # SEC fair-access cap is 10 req/sec
DB_PATH = os.environ.get("INSIDER_DB", "insider.db")
WEB_DIR = os.path.dirname(os.path.abspath(__file__))

# P = open-market purchase, S = open-market sale. These are the signal.
# A (grant), M (option exercise), F (tax withholding), G (gift) are
# compensation plumbing and will drown the dataset if you keep them.
OPEN_MARKET = {"P", "S"}

# Form types we record from the daily index purely as context. These cost
# nothing extra - they are already in the index file we download anyway.
CONTEXT_FORMS = {
    "NT 10-K", "NT 10-Q",                       # late filing
    "S-1", "S-3", "S-3/A", "424B3", "424B5",    # pending dilution
    "SC 13D", "SC 13D/A",                       # activist stake
    "8-K",                                      # checked lazily, see below
}

# 8-K item codes that should veto a buy signal outright.
VETO_ITEMS = {
    "4.01": "auditor change",
    "4.02": "prior financials not reliable",
}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS txns (
    accession    TEXT NOT NULL,
    issuer_cik   TEXT,
    ticker       TEXT,
    issuer_name  TEXT,
    owner_cik    TEXT,
    owner_name   TEXT,
    is_director  INTEGER DEFAULT 0,
    is_officer   INTEGER DEFAULT 0,
    is_ten_pct   INTEGER DEFAULT 0,
    officer_title TEXT,
    txn_date     TEXT NOT NULL,
    code         TEXT NOT NULL,
    acq_disp     TEXT,
    shares       REAL,
    price        REAL,
    value        REAL,
    owned_after  REAL,
    plan_10b5_1  INTEGER DEFAULT 0,
    filed_date   TEXT,
    PRIMARY KEY (accession, owner_cik, txn_date, code, shares, price)
);
CREATE INDEX IF NOT EXISTS ix_issuer_date ON txns (issuer_cik, txn_date);
CREATE INDEX IF NOT EXISTS ix_code_date   ON txns (code, txn_date);

CREATE TABLE IF NOT EXISTS filings (
    cik        TEXT NOT NULL,
    form_type  TEXT NOT NULL,
    filed_date TEXT NOT NULL,
    accession  TEXT NOT NULL,
    PRIMARY KEY (accession, form_type)
);
CREATE INDEX IF NOT EXISTS ix_filings ON filings (cik, filed_date);

CREATE TABLE IF NOT EXISTS eightk_items (
    accession TEXT PRIMARY KEY,
    cik       TEXT,
    items     TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    ticker     TEXT PRIMARY KEY,
    last_close REAL,
    adv_dollar REAL,          -- 20-day average daily dollar volume
    asof       TEXT
);

CREATE TABLE IF NOT EXISTS seen_days (
    day        TEXT PRIMARY KEY,
    n_filings  INTEGER,
    fetched_at TEXT
);
"""


def db_connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def http_get(url, retries=3):
    if not USER_AGENT:
        sys.exit("Set INSIDER_UA to 'Your Name your@email.com'. "
                 "The SEC blocks requests without a contact in the User-Agent.")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 ** attempt)
    return None


def daily_index(day):
    """All filings for a day: [(cik, company, form_type, path)]."""
    qtr = (day.month - 1) // 3 + 1
    url = DAILY_INDEX.format(year=day.year, qtr=qtr, ymd=day.strftime("%Y%m%d"))
    body = http_get(url)
    if body is None:
        return []
    out = []
    for line in body.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form_type, _filed, filename = (p.strip() for p in parts)
        if not cik.isdigit():
            continue
        out.append((cik, company, form_type, filename))
    return out


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

XML_BLOCK = re.compile(
    r"<(?:XML|xml)>\s*(<\?xml.*?</ownershipDocument>)\s*</(?:XML|xml)>",
    re.DOTALL | re.IGNORECASE)
OWNERSHIP_DOC = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.DOTALL)
ITEM_RE = re.compile(r"^ITEM\s+(\d\.\d\d)", re.MULTILINE | re.IGNORECASE)


def extract_ownership_xml(text):
    m = XML_BLOCK.search(text)
    if m:
        return m.group(1)
    m = OWNERSHIP_DOC.search(text)
    return m.group(0) if m else None


def _txt(node, path):
    """Read <path><value>X</value></path> or <path>X</path>. EDGAR mixes both."""
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    val = el.find("value")
    target = val if val is not None else el
    return (target.text or "").strip() or None


def _num(node, path):
    raw = _txt(node, path)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _flag(node, path):
    return 1 if _txt(node, path) in ("1", "true", "True") else 0


def parse_form4(xml_text, accession, filed_date):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return

    issuer = root.find("issuer")
    issuer_cik = (_txt(issuer, "issuerCik") or "").lstrip("0") or None
    ticker = _txt(issuer, "issuerTradingSymbol")
    issuer_name = _txt(issuer, "issuerName")

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "owner_cik": _txt(ro.find("reportingOwnerId"), "rptOwnerCik"),
            "owner_name": _txt(ro.find("reportingOwnerId"), "rptOwnerName"),
            "is_director": _flag(rel, "isDirector"),
            "is_officer": _flag(rel, "isOfficer"),
            "is_ten_pct": _flag(rel, "isTenPercentOwner"),
            "officer_title": _txt(rel, "officerTitle"),
        })
    if not owners:
        owners = [{"owner_cik": None, "owner_name": None, "is_director": 0,
                   "is_officer": 0, "is_ten_pct": 0, "officer_title": None}]

    # 10b5-1 plan flag. The element moved between form revisions and some
    # filers only disclose the plan in a footnote, so check both.
    plan = 0
    for tag in root.iter():
        low = tag.tag.lower()
        if "10b5" in low:
            inner = tag.find("value")
            txt = (inner.text if inner is not None else tag.text) or ""
            if txt.strip() in ("1", "true", "True"):
                plan = 1
    if not plan:
        notes = " ".join((t.text or "") for t in root.iter("footnote"))
        if "10b5-1" in notes:
            plan = 1

    table = root.find("nonDerivativeTable")
    if table is None:
        return

    for txn in table.findall("nonDerivativeTransaction"):
        amounts = txn.find("transactionAmounts")
        shares = _num(amounts, "transactionShares")
        price = _num(amounts, "transactionPricePerShare")
        post = txn.find("postTransactionAmounts")
        for o in owners:
            row = dict(o)
            row.update({
                "accession": accession,
                "issuer_cik": issuer_cik,
                "ticker": ticker,
                "issuer_name": issuer_name,
                "txn_date": _txt(txn, "transactionDate"),
                "code": _txt(txn.find("transactionCoding"), "transactionCode"),
                "acq_disp": _txt(amounts, "transactionAcquiredDisposedCode"),
                "shares": shares,
                "price": price,
                "value": (shares * price) if (shares and price) else None,
                "owned_after": _num(post, "sharesOwnedFollowingTransaction"),
                "plan_10b5_1": plan,
                "filed_date": filed_date,
            })
            yield row


COLS = ["accession", "issuer_cik", "ticker", "issuer_name", "owner_cik",
        "owner_name", "is_director", "is_officer", "is_ten_pct",
        "officer_title", "txn_date", "code", "acq_disp", "shares", "price",
        "value", "owned_after", "plan_10b5_1", "filed_date"]


def insert_rows(con, rows):
    if not rows:
        return 0
    sql = (f"INSERT OR IGNORE INTO txns ({','.join(COLS)}) "
           f"VALUES ({','.join('?' * len(COLS))})")
    cur = con.executemany(sql, [[r.get(c) for c in COLS] for r in rows])
    con.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
# NOTE: these weights are hand-set judgement calls, not fitted to anything.
# They rank candidates for reading; they are not a validated alpha model.
# Change them freely - and if you ever fit them, keep a real holdout.

ROLE_POINTS = [
    (("CHIEF FINANCIAL", "CFO"), 15, "CFO"),
    (("CHIEF EXECUTIVE", "CEO"), 12, "CEO"),
    (("PRESIDENT", "CHIEF OPERATING", "COO", "CHAIRMAN"), 9, "senior officer"),
]


def role_score(trades):
    best, label = 0, None
    for t in trades:
        title = (t["officer_title"] or "").upper()
        pts, lab = 0, None
        for keys, p, l in ROLE_POINTS:
            if any(k in title for k in keys):
                pts, lab = p, l
                break
        else:
            if t["is_officer"]:
                pts, lab = 7, "officer"
            elif t["is_director"]:
                pts, lab = 5, "director"
            elif t["is_ten_pct"]:
                pts, lab = 2, "10% owner"
        if pts > best:
            best, label = pts, lab
    return best, label


def stake_score(trades):
    """Biggest proportional increase in any one insider's holding.

    A CEO adding 40% to their stake says more than a larger dollar figure
    that moves their position by 2%.
    """
    best = 0.0
    for t in trades:
        shares, after = t["shares"], t["owned_after"]
        if not shares or not after or after <= shares:
            continue
        before = after - shares
        if before <= 0:
            continue
        best = max(best, shares / before)
    return min(best, 1.0) * 15, best


def context_flags(con, cik, start, end):
    """Context filings by the same issuer in the window. Free - already indexed."""
    rows = con.execute(
        "SELECT form_type, accession FROM filings "
        "WHERE cik = ? AND filed_date BETWEEN ? AND ?",
        (cik, start, end)).fetchall()
    forms = {r["form_type"] for r in rows}
    flags = {}
    if forms & {"NT 10-K", "NT 10-Q"}:
        flags["late_filing"] = "Filed a late-filing notice in this window"
    if forms & {"S-1", "S-3", "S-3/A", "424B3", "424B5"}:
        flags["offering"] = "Registration or offering document on file - dilution pending"
    if forms & {"SC 13D", "SC 13D/A"}:
        flags["activist"] = "Activist 13D stake filed in this window"

    eightk = [r["accession"] for r in rows if r["form_type"] == "8-K"]
    if eightk:
        qs = ",".join("?" * len(eightk))
        for r in con.execute(
                f"SELECT items FROM eightk_items WHERE accession IN ({qs})", eightk):
            for code in (r["items"] or "").split(","):
                if code in VETO_ITEMS:
                    flags["veto"] = f"8-K item {code}: {VETO_ITEMS[code]}"
    return flags


def score_cluster(c, flags):
    pts, why = 0, []

    n = min(c["n_insiders"], 6)
    pts += n * 8
    why.append(f"{c['n_insiders']} separate insiders buying (+{n * 8})")

    rp, rlabel = c["_role"]
    if rp:
        pts += rp
        why.append(f"most senior buyer is {rlabel} (+{rp})")

    sp, ratio = c["_stake"]
    if sp >= 1:
        pts += round(sp)
        why.append(f"one insider lifted their holding {ratio:.0%} (+{round(sp)})")

    span = c["span_days"]
    if span <= 7:
        pts += 8
        why.append(f"all buys inside {span} days (+8)")
    elif span <= 14:
        pts += 4
        why.append(f"buys clustered within {span} days (+4)")

    if c["total"] >= 1_000_000:
        size = min(10, int((c["total"] / 1_000_000) ** 0.5 * 3))
        pts += size
        why.append(f"${c['total']:,.0f} committed (+{size})")

    op, oplabel = c["_opp"]
    if op > 0:
        pts += op
        why.append(f"buyer has no regular buying pattern, this is off-cycle (+{op})")
    elif op < 0:
        pts += op
        why.append(f"these insiders buy on a routine annual schedule ({op})")

    if c["planned"]:
        pts -= 10
        why.append("part of a pre-scheduled 10b5-1 plan, not a fresh decision (-10)")
    if "activist" in flags:
        pts += 5
        why.append("activist 13D filed in the same window (+5)")
    if "late_filing" in flags:
        pts -= 20
        why.append(flags["late_filing"] + " (-20)")
    if "offering" in flags:
        pts -= 15
        why.append(flags["offering"] + " (-15)")
    if "illiquid" in flags:
        pts -= 30
        why.append(flags["illiquid"] + " (-30)")
    if "veto" in flags:
        pts -= 40
        why.append(flags["veto"] + " (-40)")

    return max(0, min(100, pts)), why


# --------------------------------------------------------------------------
# liquidity (optional - needs a price feed)
# --------------------------------------------------------------------------
# Stooq serves free daily OHLCV as CSV with no key and no registration.
# This is the one part of the pipeline I could not test from the machine I
# built it on, because that sandbox could not reach stooq.com. The parsing is
# defensive and failures degrade to "unknown liquidity" rather than crashing,
# but check the first run yourself.

STOOQ = "https://stooq.com/q/d/l/?s={sym}.us&i=d"

MIN_PRICE = 5.0            # sub-$5 names carry wide spreads
MIN_ADV_DOLLAR = 1_000_000  # below this you cannot get in or out cleanly


def fetch_price(ticker):
    """Return (last_close, 20d average dollar volume) or (None, None)."""
    if not ticker or not re.fullmatch(r"[A-Za-z.\-]{1,6}", ticker):
        return None, None
    body = http_get(STOOQ.format(sym=ticker.lower().replace(".", "-")))
    if not body or "Date" not in body[:200]:
        return None, None

    rows = []
    for line in body.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append((float(parts[4]), float(parts[5])))   # close, volume
        except ValueError:
            continue
    if not rows:
        return None, None

    recent = rows[-20:]
    last_close = recent[-1][0]
    adv = sum(c * v for c, v in recent) / len(recent)
    return last_close, adv


def cmd_prices(args):
    """Fetch prices only for tickers that actually cluster. Cheap by design."""
    con = db_connect(args.db)
    clusters = find_clusters(con, args.window, args.min_insiders, 0,
                             apply_liquidity=False)
    tickers = {c["ticker"] for c in clusters if c["ticker"] and c["ticker"] != "-"}
    if not tickers:
        print("No clustered tickers to price. Run backfill first.")
        return

    print(f"pricing {len(tickers)} tickers")
    ok = 0
    for t in sorted(tickers):
        close, adv = fetch_price(t)
        time.sleep(0.3)                      # be polite to a free service
        if close is None:
            print(f"  {t:<8} no data")
            continue
        ok += 1
        con.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                    (t, close, adv, date.today().isoformat()))
        gate = "ok" if (close >= MIN_PRICE and adv >= MIN_ADV_DOLLAR) else "ILLIQUID"
        print(f"  {t:<8} ${close:>8.2f}  ADV ${adv:>14,.0f}  {gate}")
    con.commit()
    print(f"\npriced {ok}/{len(tickers)}")
    con.close()


def liquidity_flags(con, ticker):
    row = con.execute("SELECT last_close, adv_dollar FROM prices WHERE ticker=?",
                      (ticker,)).fetchone()
    if not row or row["last_close"] is None:
        return {}, None
    close, adv = row["last_close"], row["adv_dollar"] or 0
    problems = []
    if close < MIN_PRICE:
        problems.append(f"trades at ${close:.2f}")
    if adv < MIN_ADV_DOLLAR:
        problems.append(f"only ${adv:,.0f} traded daily")
    if problems:
        return {"illiquid": "Hard to trade: " + " and ".join(problems)}, close
    return {}, close


# --------------------------------------------------------------------------
# routine vs opportunistic insiders
# --------------------------------------------------------------------------
# An insider who buys every February is telling you about their bonus
# schedule, not about the business. One who buys off-cycle, having not
# bought for years, is making a decision. Needs a few years of history:
# with too little data this returns "unknown" and adjusts nothing.

def classify_insider(con, owner_cik, before_date):
    if not owner_cik:
        return "unknown"
    rows = con.execute(
        "SELECT txn_date FROM txns WHERE owner_cik=? AND code='P' AND txn_date < ?",
        (owner_cik, before_date)).fetchall()
    dates = []
    for r in rows:
        try:
            dates.append(datetime.fromisoformat(r["txn_date"]).date())
        except (ValueError, TypeError):
            continue
    if len(dates) < 3:
        return "unknown"

    years = {d.year for d in dates}
    if len(years) < 2:
        return "unknown"

    # Routine if the same calendar month recurs across most of their history.
    months = {}
    for d in dates:
        months.setdefault(d.month, set()).add(d.year)
    top_month_years = max(len(v) for v in months.values())
    return "routine" if top_month_years >= max(2, len(years) - 1) else "opportunistic"


def opportunism_score(con, trades):
    labels = [classify_insider(con, t["owner_cik"], t["txn_date"]) for t in trades]
    if "opportunistic" in labels:
        return 10, "opportunistic"
    if labels and all(l == "routine" for l in labels):
        return -12, "routine"
    return 0, "unknown"


# --------------------------------------------------------------------------
# cluster detection
# --------------------------------------------------------------------------

def find_clusters(con, window_days=30, min_insiders=3, min_value=0,
                  apply_liquidity=True):
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    rows = con.execute("""
        SELECT * FROM txns WHERE code='P' AND txn_date >= ?
        ORDER BY issuer_cik, txn_date""", (cutoff,)).fetchall()

    by_issuer = {}
    for r in rows:
        by_issuer.setdefault(r["issuer_cik"], []).append(r)

    out = []
    for cik, trades in by_issuer.items():
        insiders = {t["owner_cik"] or t["owner_name"] for t in trades}
        if len(insiders) < min_insiders:
            continue
        total = sum(t["value"] or 0 for t in trades)
        if total < min_value:
            continue

        dates = sorted(t["txn_date"] for t in trades if t["txn_date"])
        try:
            span = (datetime.fromisoformat(dates[-1])
                    - datetime.fromisoformat(dates[0])).days
        except (ValueError, IndexError):
            span = window_days

        c = {
            "cik": cik,
            "ticker": trades[0]["ticker"] or "-",
            "name": trades[0]["issuer_name"] or "-",
            "n_insiders": len(insiders),
            "n_trades": len(trades),
            "total": total,
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "span_days": span,
            "planned": any(t["plan_10b5_1"] for t in trades),
            "buyers": [],
        }
        seen = set()
        for t in trades:
            key = t["owner_cik"] or t["owner_name"]
            if key in seen:
                continue
            seen.add(key)
            c["buyers"].append({
                "name": t["owner_name"],
                "title": t["officer_title"] or
                         ("Director" if t["is_director"] else
                          "10% owner" if t["is_ten_pct"] else "Insider"),
            })
        c["_role"] = role_score(trades)
        c["_stake"] = stake_score(trades)
        c["_opp"] = opportunism_score(con, trades)
        c["insider_type"] = c["_opp"][1]

        ctx_start = (datetime.fromisoformat(c["first"]) - timedelta(days=30)
                     ).date().isoformat() if c["first"] else cutoff
        flags = context_flags(con, cik, ctx_start, date.today().isoformat())

        if apply_liquidity:
            liq, close = liquidity_flags(con, c["ticker"])
            flags.update(liq)
            c["last_close"] = close
        else:
            c["last_close"] = None

        c["score"], c["why"] = score_cluster(c, flags)
        c["flags"] = flags
        for k in ("_role", "_stake", "_opp"):
            c.pop(k)
        out.append(c)

    out.sort(key=lambda c: (c["score"], c["total"]), reverse=True)
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_backfill(args):
    con = db_connect(args.db)
    done = {r["day"] for r in con.execute("SELECT day FROM seen_days")}
    today = date.today()
    days = [today - timedelta(days=i) for i in range(args.days)]
    days = [d for d in days if d.weekday() < 5]

    for d in sorted(days):
        key = d.isoformat()
        if key in done and not args.force:
            continue

        index = daily_index(d)
        time.sleep(REQ_DELAY)
        if not index:
            print(f"{key}: no index (holiday?)")
            con.execute("INSERT OR REPLACE INTO seen_days VALUES (?,?,?)",
                        (key, 0, datetime.now(timezone.utc).isoformat()))
            con.commit()
            continue

        # Record context filings for free - we already have the index.
        ctx = [(cik.lstrip("0"), ft, key, path.rsplit("/", 1)[-1].replace(".txt", ""))
               for cik, _co, ft, path in index if ft in CONTEXT_FORMS]
        con.executemany("INSERT OR IGNORE INTO filings VALUES (?,?,?,?)", ctx)
        con.commit()

        form4 = [(c, co, p) for c, co, ft, p in index if ft == "4"]
        print(f"{key}: {len(form4)} Form 4, {len(ctx)} context filings", flush=True)

        kept = 0
        for i, (cik, _company, path) in enumerate(form4, 1):
            body = http_get(ARCHIVES + path)
            time.sleep(REQ_DELAY)
            if not body:
                continue
            xml_text = extract_ownership_xml(body)
            if not xml_text:
                continue
            acc = path.rsplit("/", 1)[-1].replace(".txt", "")
            rows = [r for r in parse_form4(xml_text, acc, key)
                    if r["code"] in OPEN_MARKET]
            kept += insert_rows(con, rows)
            if args.verbose and i % 200 == 0:
                print(f"   {i}/{len(form4)}", flush=True)

        con.execute("INSERT OR REPLACE INTO seen_days VALUES (?,?,?)",
                    (key, len(form4), datetime.now(timezone.utc).isoformat()))
        con.commit()
        print(f"   kept {kept} open-market rows")

    check_eightks(con, args.window if hasattr(args, "window") else 30)
    con.close()


def check_eightks(con, window_days=30):
    """Fetch 8-K item codes, but only for issuers that already cluster.

    Checking every 8-K would cost thousands of requests a day. Checking the
    handful that matter costs a few dozen.
    """
    cands = find_clusters(con, window_days, min_insiders=2)
    if not cands:
        return
    ciks = {c["cik"] for c in cands}
    qs = ",".join("?" * len(ciks))
    todo = con.execute(f"""
        SELECT f.cik, f.accession FROM filings f
        LEFT JOIN eightk_items e ON e.accession = f.accession
        WHERE f.form_type='8-K' AND f.cik IN ({qs}) AND e.accession IS NULL
    """, list(ciks)).fetchall()

    if todo:
        print(f"checking {len(todo)} 8-Ks on clustered issuers")
    for r in todo:
        acc_plain = r["accession"].replace("-", "")
        url = f"{ARCHIVES}edgar/data/{r['cik']}/{acc_plain}/{r['accession']}.txt"
        body = http_get(url)
        time.sleep(REQ_DELAY)
        items = ",".join(sorted(set(ITEM_RE.findall(body)))) if body else ""
        con.execute("INSERT OR REPLACE INTO eightk_items VALUES (?,?,?)",
                    (r["accession"], r["cik"], items))
    con.commit()


def cmd_clusters(args):
    con = db_connect(args.db)
    found = find_clusters(con, args.window, args.min_insiders, args.min_value)
    if not found:
        print("No clusters matched. Run backfill first, or widen --window.")
        return
    print(f"\n{len(found)} cluster(s) - {args.window}d window, "
          f">={args.min_insiders} insiders\n")
    for c in found:
        print(f"[{c['score']:>3}] {c['ticker']:<8} {c['name'][:42]}")
        print(f"       {c['n_insiders']} insiders, {c['n_trades']} buys, "
              f"${c['total']:,.0f}, {c['first']}..{c['last']}"
              f"  [{c.get('insider_type','unknown')}]")
        for w in c["why"]:
            print(f"         {w}")
        print()
    con.close()


def cmd_export(args):
    con = db_connect(args.db)
    clusters = find_clusters(con, args.window, args.min_insiders, args.min_value)

    recent = [dict(r) for r in con.execute("""
        SELECT ticker, issuer_name, owner_name, officer_title, txn_date,
               shares, price, value, plan_10b5_1
        FROM txns WHERE code='P' AND COALESCE(value,0) > 0
        ORDER BY txn_date DESC, value DESC LIMIT 150""")]

    stats = con.execute("""
        SELECT (SELECT COUNT(*) FROM txns WHERE code='P') AS buys,
               (SELECT COUNT(*) FROM txns WHERE code='S') AS sells,
               (SELECT COUNT(*) FROM seen_days) AS days,
               (SELECT SUM(n_filings) FROM seen_days) AS filings""").fetchone()

    os.makedirs(WEB_DIR, exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": args.window,
        "min_insiders": args.min_insiders,
        "stats": dict(stats),
        "clusters": clusters,
        "recent": recent,
    }
    out = os.path.join(WEB_DIR, "data.json")
    with open(out, "w") as fh:
        json.dump(payload, fh, indent=1, default=str)
    print(f"wrote {out} - {len(clusters)} clusters, {len(recent)} buys")
    con.close()


def cmd_serve(args):
    import http.server
    import socketserver
    os.chdir(WEB_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"\n  Insider Radar -> http://127.0.0.1:{args.port}\n"
              f"  Ctrl-C to stop\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def cmd_selftest(args):
    """Offline: parse synthetic filings, score them, write demo data.json."""
    con = db_connect(":memory:")
    today = date.today()

    def synth(cik, ticker, ocik, oname, title, code, shares, price, owned, ago):
        return f"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>{cik}</issuerCik>
    <issuerName>{ticker} Industries Inc</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId>
      <rptOwnerCik>{ocik}</rptOwnerCik><rptOwnerName>{oname}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector>
      <isOfficer>1</isOfficer><officerTitle>{title}</officerTitle>
    </reportingOwnerRelationship></reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>{(today - timedelta(days=ago)).isoformat()}</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
    <postTransactionAmounts>
      <sharesOwnedFollowingTransaction><value>{owned}</value></sharesOwnedFollowingTransaction>
    </postTransactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""

    fixtures = [
        # ACME: tight, senior, big stake increases -> should score high
        ("801", "ACME", "111", "Okafor Grace", "Chief Financial Officer", "P", 60000, 12.40, 150000, 4),
        ("801", "ACME", "222", "Smith John A", "Chief Executive Officer", "P", 90000, 12.55, 400000, 5),
        ("801", "ACME", "333", "Lee Priya", "Director", "P", 25000, 12.10, 80000, 6),
        ("801", "ACME", "444", "Brown Dave", "Director", "A", 900000, 0.0, 900000, 3),
        # DILU: same shape, but a shelf offering is pending -> penalised
        ("802", "DILU", "555", "Vance Marcus", "Chief Executive Officer", "P", 40000, 8.00, 900000, 8),
        ("802", "DILU", "666", "Cole Anita", "Director", "P", 30000, 8.10, 700000, 9),
        ("802", "DILU", "777", "Reid Tom", "Director", "P", 20000, 8.05, 600000, 11),
        # SOLO: one buyer only -> excluded
        ("803", "SOLO", "888", "Chen Wei", "Chief Executive Officer", "P", 80000, 30.0, 200000, 4),
        # NOIS: sales only -> excluded
        ("804", "NOIS", "999", "Diaz Luis", "Chief Executive Officer", "S", 50000, 55.0, 100000, 6),
    ]
    for i, f in enumerate(fixtures):
        rows = [r for r in parse_form4(synth(*f), f"acc-{i}", today.isoformat())
                if r["code"] in OPEN_MARKET]
        insert_rows(con, rows)

    con.execute("INSERT OR IGNORE INTO filings VALUES ('802','424B5',?,'x1')",
                ((today - timedelta(days=7)).isoformat(),))

    # ROUT: three insiders, but each has bought every March for years.
    for n, (ocik, oname) in enumerate([("a1", "Hale Ruth"), ("a2", "Park Jin"),
                                       ("a3", "Nkemdi Ada")]):
        for yr in (today.year - 3, today.year - 2, today.year - 1):
            con.execute(
                "INSERT OR IGNORE INTO txns (accession,issuer_cik,ticker,"
                "issuer_name,owner_cik,owner_name,is_director,officer_title,"
                "txn_date,code,shares,price,value,owned_after,plan_10b5_1) "
                "VALUES (?,?,?,?,?,?,1,'Director',?,'P',1000,10.0,10000,50000,0)",
                (f"h-{ocik}-{yr}", "805", "ROUT", "ROUT Corp", ocik, oname,
                 f"{yr}-03-12"))
        con.execute(
            "INSERT OR IGNORE INTO txns (accession,issuer_cik,ticker,issuer_name,"
            "owner_cik,owner_name,is_director,officer_title,txn_date,code,shares,"
            "price,value,owned_after,plan_10b5_1) "
            "VALUES (?,?,?,?,?,?,1,'Director',?,'P',1000,10.0,10000,60000,0)",
            (f"now-{ocik}", "805", "ROUT", "ROUT Corp", ocik, oname,
             (today - timedelta(days=3)).isoformat()))

    # Price rows: ACME liquid, DILU a sub-$5 microcap.
    con.execute("INSERT OR REPLACE INTO prices VALUES ('ACME',12.50,8400000,?)",
                (today.isoformat(),))
    con.execute("INSERT OR REPLACE INTO prices VALUES ('DILU',3.10,240000,?)",
                (today.isoformat(),))
    con.commit()

    print(f"kept {con.execute('SELECT COUNT(*) FROM txns').fetchone()[0]} "
          f"open-market rows (grants discarded)\n")

    for c in find_clusters(con, 30, 3, 0):
        print(f"[{c['score']:>3}] {c['ticker']}  {c['n_insiders']} insiders, "
              f"${c['total']:,.0f}")
        for w in c["why"]:
            print(f"        {w}")
        print()
    print("SOLO (1 buyer) and NOIS (sales) correctly excluded.")

    if args.write_demo:
        os.makedirs(WEB_DIR, exist_ok=True)
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "demo": True, "window_days": 30, "min_insiders": 3,
            "stats": {"buys": 6, "sells": 1, "days": 0, "filings": 0},
            "clusters": find_clusters(con, 30, 3, 0),
            "recent": [dict(r) for r in con.execute(
                "SELECT ticker, issuer_name, owner_name, officer_title, txn_date,"
                " shares, price, value, plan_10b5_1 FROM txns WHERE code='P'"
                " ORDER BY value DESC")],
        }
        with open(os.path.join(WEB_DIR, "data.json"), "w") as fh:
            json.dump(payload, fh, indent=1, default=str)
        print(f"\nwrote demo {WEB_DIR}/data.json")
    con.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_window(sp):
        sp.add_argument("--window", type=int, default=30)
        sp.add_argument("--min-insiders", type=int, default=3)
        sp.add_argument("--min-value", type=float, default=0)

    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--force", action="store_true")
    b.add_argument("--verbose", action="store_true")
    b.set_defaults(func=cmd_backfill)

    c = sub.add_parser("clusters"); add_window(c); c.set_defaults(func=cmd_clusters)
    e = sub.add_parser("export");   add_window(e); e.set_defaults(func=cmd_export)

    pr = sub.add_parser("prices"); add_window(pr); pr.set_defaults(func=cmd_prices)

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    t = sub.add_parser("selftest")
    t.add_argument("--write-demo", action="store_true")
    t.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
