"""Precompute analytics payloads for the four UI preset windows into
analytics_cache.json (committed, ships with the deployment).

Deployed /api/analytics serves presets from this file — instant, no Turso
row-reads, and the app's read-only DB token never needs temp-table (write)
permission. Re-run after regenerating accounts.db:

    python3.13 build_analytics_cache.py
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "api"))

import analytics  # noqa: E402

PRESET_MONTHS = (3, 6, 12)  # trailing windows; plus the full history


def month_str(serial: int) -> str:
    return f"{serial // 12:04d}-{serial % 12 + 1:02d}"


def main() -> None:
    t0 = time.time()
    lo, hi = analytics._data_bounds()
    latest_serial = int(hi[:4]) * 12 + (int(hi[5:7]) - 1)
    windows = [(lo, hi)]
    for n in PRESET_MONTHS:
        windows.append((month_str(latest_serial - (n - 1)), hi))

    out = {"bounds": [lo, hi], "generated_from": "accounts.db", "windows": {}}
    for from_m, to_m in windows:
        payload = analytics.compute(from_m, to_m)
        out["windows"][f"{from_m}..{to_m}"] = payload
        print(f"  {from_m}..{to_m}: {payload['computed_in_ms']}ms")

    dest = HERE / "analytics_cache.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {dest.name} ({dest.stat().st_size / 1024:.0f} KB, "
          f"{len(out['windows'])} windows) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
