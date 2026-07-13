"""AccountSearch AI — analytics endpoint (Vercel Python serverless function).

GET /api/analytics[?from=YYYY-MM&to=YYYY-MM] -> JSON payload with every
pre-built business report, computed over the requested month window (defaults
to the full history): KPIs, monthly booking/revenue trend, account flow,
customer segments (with revenue mix + per-bucket account lists for drill-down),
booking-recency buckets, Pareto concentration, churn-risk list and top accounts.
Pure aggregation over accounts.db — no AI calls. Results are cached per
(DB mtime, window) in the warm instance.
"""

import json
import re
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import _turso

DB_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "accounts.db",
    Path.cwd() / "accounts.db",
]

_CACHE = {}  # (stamp, from, to) -> payload
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _local_db():
    return next((p for p in DB_CANDIDATES if p.exists()), None)


def _connect():
    """Local accounts.db when present (dev); hosted Turso otherwise (Vercel)."""
    p = _local_db()
    if p is not None:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    if _turso.available():
        return _turso.connect()
    raise FileNotFoundError(
        "accounts.db not found and TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set.")


def _cache_stamp():
    p = _local_db()
    return p.stat().st_mtime if p else "turso"


# Precomputed preset windows (build_analytics_cache.py). This is how deployed
# analytics is served: the read-only Turso token forbids the TEMP TABLE the
# live compute needs, and presets cover everything the UI requests.
CACHE_FILE_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "analytics_cache.json",
    Path.cwd() / "analytics_cache.json",
]
_FILE_CACHE = None


def _file_cache() -> dict:
    global _FILE_CACHE
    if _FILE_CACHE is None:
        p = next((p for p in CACHE_FILE_CANDIDATES if p.exists()), None)
        _FILE_CACHE = json.loads(p.read_text()) if p else {}
    return _FILE_CACHE


SEGMENTS = [
    # (label, min avg trips per active month, max) — business thresholds:
    # ~20 good, ~30 best, ~40 elite
    ("Elite (35+/mo)", 35, None),
    ("Best (25–35/mo)", 25, 35),
    ("Good (15–25/mo)", 15, 25),
    ("Occasional (<15/mo)", None, 15),
]

RECENCY_BUCKETS = [
    ("0–1 months ago", 0, 1),
    ("2–3 months ago", 2, 3),
    ("4–6 months ago", 4, 6),
    ("7–12 months ago", 7, 12),
    ("13+ months ago", 13, 999),
]

_MS_EXPR = ("? - (CAST(substr(a.last_month,1,4) AS INTEGER)*12 "
            "+ CAST(substr(a.last_month,6,2) AS INTEGER))")


def _serial(m: str) -> int:
    return int(m[:4]) * 12 + int(m[5:7])


def _bucket_accounts(cur, cond: str, params: tuple, limit: int = 20) -> list:
    return [
        {"account_id": aid, "name": name, "revenue": round(rev, 2),
         "bookings": tb, "last_booked": lb}
        for aid, name, rev, tb, lb in cur.execute(
            "SELECT a.account_id, ac.account_name, a.total_revenue, a.total_bookings, "
            f"a.last_booked FROM acct a JOIN accounts ac ON ac.account_id = a.account_id "
            f"WHERE {cond} ORDER BY a.total_revenue DESC LIMIT {limit}", params)
    ]


def compute(from_m: str, to_m: str) -> dict:
    started = time.time()
    conn = _connect()
    cur = conn.cursor()

    # One pass over monthly_activity (window-scoped) into a temp per-account
    # table; every account-level report below reads from it.
    cur.execute("""
        CREATE TEMP TABLE acct AS
        SELECT account_id,
               SUM(bookings)      AS total_bookings,
               AVG(bookings)      AS avg_per_active_month,
               SUM(revenue)       AS total_revenue,
               SUM(profit)        AS total_profit,
               MIN(month)         AS first_month,
               MAX(month)         AS last_month,
               MAX(last_reservation_date) AS last_booked
        FROM monthly_activity WHERE month BETWEEN ? AND ? GROUP BY account_id
    """, (from_m, to_m))

    months, bookings, revenue, profit, active = [], [], [], [], []
    for m, b, r, p, a in cur.execute(
            "SELECT month, SUM(bookings), SUM(revenue), SUM(profit), COUNT(*) "
            "FROM monthly_activity WHERE month BETWEEN ? AND ? "
            "GROUP BY month ORDER BY month", (from_m, to_m)):
        months.append(m)
        bookings.append(b)
        revenue.append(round(r, 2))
        profit.append(round(p, 2))
        active.append(a)
    if not months:
        conn.close()
        raise ValueError(f"No data between {from_m} and {to_m}.")
    latest = months[-1]
    latest_serial = _serial(latest)

    total_accounts = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    ever_booked = cur.execute("SELECT COUNT(*) FROM acct").fetchone()[0]
    never = total_accounts - ever_booked
    total_rev, total_prof, total_book = sum(revenue), sum(profit), sum(bookings)

    # Segments: account count + revenue share + top accounts per bucket (the
    # per-bucket lists power instant client-side drill-down).
    segments = [{"label": "Never booked", "accounts": never, "revenue": 0.0, "top": []}]
    for label, lo, hi in SEGMENTS:
        conds, params = [], []
        if lo is not None:
            conds.append("avg_per_active_month >= ?"); params.append(lo)
        if hi is not None:
            conds.append("avg_per_active_month < ?"); params.append(hi)
        cond = " AND ".join(conds)
        n, rev = cur.execute(
            f"SELECT COUNT(*), COALESCE(SUM(total_revenue),0) FROM acct WHERE {cond}",
            params).fetchone()
        segments.append({"label": label, "accounts": n, "revenue": round(rev, 2),
                         "top": _bucket_accounts(cur, "a." + cond.replace(" AND ", " AND a."), tuple(params))})

    # Recency: months since last booking as of the window's end.
    recency = []
    for label, lo, hi in RECENCY_BUCKETS:
        cond = f"({_MS_EXPR}) BETWEEN ? AND ?"
        n = cur.execute(f"SELECT COUNT(*) FROM acct a WHERE {cond}",
                        (latest_serial, lo, hi)).fetchone()[0]
        recency.append({"bucket": label, "accounts": n,
                        "top": _bucket_accounts(cur, cond, (latest_serial, lo, hi))})
    recency.append({"bucket": "Never booked", "accounts": never, "top": []})

    # Pareto concentration: share of window revenue from the top 1% / 10% of
    # booking accounts (ranked by revenue).
    pareto = {}
    for key, frac in (("top1_pct", 0.01), ("top10_pct", 0.10)):
        n = max(1, int(ever_booked * frac))
        top_rev = cur.execute(
            "SELECT COALESCE(SUM(r),0) FROM (SELECT total_revenue AS r FROM acct "
            f"ORDER BY total_revenue DESC LIMIT {n})").fetchone()[0]
        pareto[key] = round(100 * top_rev / total_rev, 1) if total_rev else 0.0

    # Account flow within the window: first booking (new) vs last booking
    # followed by 3+ months of silence (lost). Window edges are excluded — the
    # opening month (everyone is "new") and the final 3 months (not yet "lost").
    firsts = dict(cur.execute("SELECT first_month, COUNT(*) FROM acct GROUP BY first_month"))
    lasts = dict(cur.execute("SELECT last_month, COUNT(*) FROM acct GROUP BY last_month"))
    flow_months = months[1:-3] if len(months) > 4 else []
    flow = {"months": flow_months,
            "new": [firsts.get(m, 0) for m in flow_months],
            "lost": [lasts.get(m, 0) for m in flow_months]}

    # Churn risk: highest-revenue accounts silent 3+ months as of window end.
    churn_risk = [
        {"account_id": aid, "name": name, "revenue": round(rev, 2), "bookings": tb,
         "last_booked": lb, "months_silent": ms}
        for aid, name, rev, tb, lb, ms in cur.execute(
            "SELECT a.account_id, ac.account_name, a.total_revenue, a.total_bookings, "
            f"a.last_booked, {_MS_EXPR} AS ms "
            "FROM acct a JOIN accounts ac ON ac.account_id = a.account_id "
            f"WHERE {_MS_EXPR} >= 3 ORDER BY a.total_revenue DESC LIMIT 20",
            (latest_serial, latest_serial))
    ]

    top_accounts = [
        {"account_id": aid, "name": name, "revenue": round(rev, 2),
         "bookings": tb, "last_booked": lb}
        for aid, name, rev, tb, lb in cur.execute(
            "SELECT a.account_id, ac.account_name, a.total_revenue, a.total_bookings, a.last_booked "
            "FROM acct a JOIN accounts ac ON ac.account_id = a.account_id "
            "ORDER BY a.total_revenue DESC LIMIT 10")
    ]

    payload = {
        "kpis": {
            "total_accounts": total_accounts,
            "active_last_month": active[-1],
            "total_bookings": total_book,
            "total_revenue": round(total_rev, 2),
            "total_profit": round(total_prof, 2),
            "avg_revenue_per_trip": round(total_rev / total_book, 2) if total_book else 0,
            "never_booked": never,
            "first_month": months[0],
            "latest_month": latest,
        },
        "monthly": {"months": months, "bookings": bookings, "revenue": revenue,
                    "profit": profit, "active_accounts": active},
        "segments": segments,
        "recency": recency,
        "pareto": pareto,
        "flow": flow,
        "churn_risk": churn_risk,
        "top_accounts": top_accounts,
        "computed_in_ms": int((time.time() - started) * 1000),
    }
    conn.close()
    return payload


_BOUNDS = None


def _data_bounds() -> tuple:
    global _BOUNDS
    if _BOUNDS is None:
        if _local_db() is None and _file_cache().get("bounds"):
            _BOUNDS = tuple(_file_cache()["bounds"])
        else:
            conn = _connect()
            try:
                _BOUNDS = conn.execute("SELECT MIN(month), MAX(month) FROM monthly_activity").fetchone()
            finally:
                conn.close()
    return _BOUNDS


def get_payload(from_m: str = None, to_m: str = None) -> dict:
    lo, hi = _data_bounds()
    from_m = from_m if from_m and _MONTH_RE.match(from_m) else lo
    to_m = to_m if to_m and _MONTH_RE.match(to_m) else hi
    from_m, to_m = max(from_m, lo), min(to_m, hi)
    # Deployed (no local DB): serve precomputed preset windows from the bundle.
    if _local_db() is None:
        hit = _file_cache().get("windows", {}).get(f"{from_m}..{to_m}")
        if hit:
            return hit
    key = (_cache_stamp(), from_m, to_m)
    if key not in _CACHE:
        if len(_CACHE) >= 12:  # keep the warm-instance cache bounded
            _CACHE.clear()
        _CACHE[key] = compute(from_m, to_m)
    return _CACHE[key]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - Vercel convention
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            body = get_payload(qs.get("from", [None])[0], qs.get("to", [None])[0])
            status = 200
        except Exception as e:  # noqa: BLE001
            body, status = {"error": str(e)[:500]}, 500
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)
