"""Generate accounts.db — synthetic monthly booking activity for 500k accounts.

Reads rawdata.csv (account_id, account_name) and produces a SQLite database with:

  accounts(id, account_id, account_name, tier)
  monthly_activity(account_id, month, bookings, revenue, profit, last_reservation_date)

Rules (agreed 2026-07-12):
- 24 months of history: 2024-07 .. 2026-06.
- Rows exist ONLY for months with bookings > 0 (like a real transaction export).
- Tier mix: 15% never booked, 40% occasional (1-10 trips/mo with gaps),
  25% good (~20/mo), 10% best (~30/mo), 5% elite (~40/mo, occasionally skips
  a month), 5% churned (were active, silent for the last 6+ months).
- Per-trip economics: ~90% regular trips avg $1,000 revenue / $200 profit,
  ~10% cheap trips avg $100 / $10. Monthly totals are sampled from the
  normal approximation of the per-trip sums (statistically equivalent,
  ~20x faster than drawing every trip).
- last_reservation_date = date of the month's final booking (max of n
  uniform days), so "last booked as of month M" = the row's date, and for
  silent months it's recovered via MAX() over prior months.

Deterministic: seeded RNG, same output every run.
"""

import calendar
import csv
import random
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_CSV = HERE / "rawdata.csv"
DB_PATH = HERE / "accounts.db"

random.seed(20260712)

MONTHS = [(y, m) for y in (2024, 2025, 2026) for m in range(1, 13)
          if (y, m) >= (2024, 7) and (y, m) <= (2026, 6)]
assert len(MONTHS) == 24

TIERS = [
    # (name, share, mean bookings, sd, skip probability per month)
    ("never",      0.15, 0,  0, 1.00),
    ("occasional", 0.40, 0,  0, 0.45),   # bookings drawn uniform 1-10 instead
    ("good",       0.25, 20, 4, 0.03),
    ("best",       0.10, 30, 5, 0.05),
    ("elite",      0.05, 40, 6, 0.08),   # elite sometimes skips a month
    ("churned",    0.05, 15, 5, 0.10),   # active early, then silent >= 6 months
]

CHEAP_SHARE = 0.10
REV_REG, REV_REG_SD = 1000.0, 200.0
PROFIT_REG, PROFIT_REG_SD = 200.0, 60.0
REV_CHEAP, REV_CHEAP_SD = 100.0, 20.0
PROFIT_CHEAP, PROFIT_CHEAP_SD = 10.0, 3.0


def month_revenue(n: int) -> tuple[float, float]:
    """Sample (revenue, profit) for a month with n bookings via normal
    approximation of the sum of per-trip draws."""
    cheap = round(random.gauss(n * CHEAP_SHARE, (n * CHEAP_SHARE * (1 - CHEAP_SHARE)) ** 0.5))
    cheap = max(0, min(n, cheap))
    reg = n - cheap
    revenue = profit = 0.0
    if reg:
        revenue += max(reg * 300.0, random.gauss(reg * REV_REG, REV_REG_SD * reg ** 0.5))
        profit += max(reg * 40.0, random.gauss(reg * PROFIT_REG, PROFIT_REG_SD * reg ** 0.5))
    if cheap:
        revenue += max(cheap * 40.0, random.gauss(cheap * REV_CHEAP, REV_CHEAP_SD * cheap ** 0.5))
        profit += max(cheap * 2.0, random.gauss(cheap * PROFIT_CHEAP, PROFIT_CHEAP_SD * cheap ** 0.5))
    profit = min(profit, revenue * 0.45)  # profit can never approach revenue
    return round(revenue, 2), round(profit, 2)


def last_booking_day(year: int, month: int, n: int) -> str:
    """Day of the month's final booking: max of n uniform draws."""
    days = calendar.monthrange(year, month)[1]
    day = 1 + int((days - 1) * random.random() ** (1.0 / n))
    return f"{year:04d}-{month:02d}-{day:02d}"


def assign_tier() -> str:
    r = random.random()
    acc = 0.0
    for name, share, *_ in TIERS:
        acc += share
        if r < acc:
            return name
    return TIERS[-1][0]


def bookings_for(tier: str, mean: float, sd: float) -> int:
    if tier == "occasional":
        return random.randint(1, 10)
    return max(1, round(random.gauss(mean, sd)))


def main() -> None:
    start = time.time()
    with open(RAW_CSV, encoding="utf-8-sig", newline="") as f:
        raw = [(i, int(r[0]), r[1]) for i, r in enumerate(csv.reader(f), start=1) if r]
    print(f"{len(raw):,} accounts loaded from {RAW_CSV.name}")

    DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            account_name TEXT NOT NULL,
            tier TEXT NOT NULL
        );
        CREATE TABLE monthly_activity (
            account_id INTEGER NOT NULL,
            month TEXT NOT NULL,               -- 'YYYY-MM'
            bookings INTEGER NOT NULL,
            revenue REAL NOT NULL,
            profit REAL NOT NULL,
            last_reservation_date TEXT NOT NULL -- 'YYYY-MM-DD'
        );
    """)

    tier_params = {name: (mean, sd, skip) for name, _, mean, sd, skip in TIERS}
    accounts_rows, activity_rows, total_activity = [], [], 0

    for idx, (row_id, account_id, name) in enumerate(raw, start=1):
        tier = assign_tier()
        accounts_rows.append((row_id, account_id, name, tier))
        if tier != "never":
            mean, sd, skip = tier_params[tier]
            # churned accounts go silent after a cutoff month, >= 6 months before end
            cutoff = random.randint(4, len(MONTHS) - 6) if tier == "churned" else len(MONTHS)
            for m_idx, (y, m) in enumerate(MONTHS):
                if m_idx >= cutoff or random.random() < skip:
                    continue
                n = bookings_for(tier, mean, sd)
                revenue, profit = month_revenue(n)
                activity_rows.append((account_id, f"{y:04d}-{m:02d}", n, revenue,
                                      profit, last_booking_day(y, m, n)))
        if len(activity_rows) >= 200_000:
            conn.executemany("INSERT INTO monthly_activity VALUES (?,?,?,?,?,?)", activity_rows)
            total_activity += len(activity_rows)
            activity_rows.clear()
        if idx % 100_000 == 0:
            print(f"  {idx:,} accounts processed ({time.time() - start:.0f}s)")

    conn.executemany("INSERT INTO monthly_activity VALUES (?,?,?,?,?,?)", activity_rows)
    total_activity += len(activity_rows)
    conn.executemany("INSERT INTO accounts VALUES (?,?,?,?)", accounts_rows)
    conn.commit()

    print("indexing...")
    conn.executescript("""
        CREATE UNIQUE INDEX idx_acc_account_id ON accounts(account_id);
        CREATE INDEX idx_ma_account_month ON monthly_activity(account_id, month);
        CREATE INDEX idx_ma_month ON monthly_activity(month);
    """)
    conn.commit()
    # Turso's --from-file upload requires WAL mode (journal_mode=OFF was only
    # for build speed).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()

    size_mb = DB_PATH.stat().st_size / 1024 / 1024
    print(f"done: {len(raw):,} accounts, {total_activity:,} activity rows, "
          f"{size_mb:,.0f} MB, {time.time() - start:.0f}s -> {DB_PATH.name}")


if __name__ == "__main__":
    main()
