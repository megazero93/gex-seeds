#!/usr/bin/env python3
"""
GEX Profile Pine Seeds Generator

Fetches options greeks + open interest from ThetaData Terminal (must be running
on localhost:25503) and writes OHLCV CSV files to data/ for TradingView Pine Seeds.

Each CSV represents one data series read via request.seed() in Pine Script.
Exports per ticker (39 seed symbols, within Pine Script's 40-call limit):
  - 1 flip level
  - 3 call walls + 3 put walls (primary + 2 secondary each)
  - 2 net scalars: GEX_NET_TOTAL, VEX_NET_TOTAL
  - 15 histogram strikes + 15 histogram GEX values

Usage:
    python generate_gex.py                  # updates DEFAULT_SYMS
    python generate_gex.py SPY QQQ SPX      # override symbols via CLI
    python generate_gex.py SPY --exps 6     # use 6 nearest expirations
"""

import asyncio
import csv
import datetime
import logging
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ─── Configuration ─────────────────────────────────────────────────────────────
BASE_URL      = "http://127.0.0.1:25503/v3"
DATA_DIR      = Path(__file__).parent / "data"
TOP_N         = 15    # GEX levels to export — keeps request.seed() calls ≤ 40 in Pine Script
NUM_EXPS      = 4     # Nearest expirations to aggregate (4 captures all meaningful near-term gamma)
STRIKE_RANGE  = 0.10  # ±10 % from spot
DEFAULT_SYMS  = ["SPY", "QQQ"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─── ThetaData REST Client ──────────────────────────────────────────────────────

class ThetaClient:
    """
    ThetaData v3 REST client.

    The v3 bulk response format is:
        {
          "header": {
            "format": ["ms_of_day", "bid", "ask", "delta", "gamma", ...],
            "underlying_price": 545.23,   ← spot price lives HERE
            ...
          },
          "response": [
            {
              "contract": {"root": "SPY", "expiration": 20260414, "strike": 540000, "right": "C"},
              "data": [[36000000, 4.5, 4.6, 0.4523, 0.0234, ...], ...]
            },
            ...
          ]
        }

    _parse() flattens this into a list of plain dicts, injecting every header
    metadata field (including underlying_price) into each row.
    """

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=60.0)

    # ── Response parser ────────────────────────────────────────────────────────

    def _parse_data_block(self, data: list, col_names: list, extra: dict) -> List[Dict]:
        rows = []
        for row in data:
            if isinstance(row, (list, tuple)) and col_names:
                d = dict(zip(col_names, row))
            elif isinstance(row, dict):
                d = dict(row)
            else:
                continue
            d.update(extra)
            rows.append(d)
        return rows

    def _parse(self, raw) -> List[Dict]:
        if not isinstance(raw, (dict, list)):
            return []

        # Top-level dict — may carry a shared header for all response items
        if isinstance(raw, dict):
            header    = raw.get("header") or {}
            col_names = header.get("format") or []
            # Everything in header except "format" is metadata (e.g. underlying_price)
            meta      = {k: v for k, v in header.items() if k != "format"}

            if "response" in raw:
                return self._parse_items(raw["response"], col_names, meta)

            if "data" in raw:
                contract = raw.get("contract") or {}
                return self._parse_data_block(raw["data"], col_names, {**contract, **meta})

            # Plain dict with no special keys — return as single-item list
            return [raw]

        # Top-level list
        if isinstance(raw, list):
            return self._parse_items(raw, [], {})

        return []

    def _parse_items(self, items: list, col_names: list, meta: dict) -> List[Dict]:
        rows = []
        for item in items:
            if not isinstance(item, dict):
                rows.append({"value": item})
                continue

            # Item may carry its own header (overrides parent header)
            item_header    = item.get("header") or {}
            item_col_names = item_header.get("format") or col_names
            item_meta      = ({k: v for k, v in item_header.items() if k != "format"}
                              if item_header else meta)

            if "data" in item:
                contract = item.get("contract") or {}
                rows.extend(self._parse_data_block(
                    item["data"], item_col_names, {**contract, **item_meta}
                ))
            else:
                d = dict(item)
                d.update(meta)
                rows.append(d)
        return rows

    # ── HTTP helper ────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: Dict[str, Any]) -> List[Dict]:
        params["format"] = "json"
        try:
            r = await self.http.get(f"{BASE_URL}{path}", params=params)
            if r.status_code == 472:   # ThetaData "no data found"
                return []
            r.raise_for_status()
            return self._parse(r.json())
        except Exception as e:
            log.warning("Request failed %s: %s", path, e)
            return []

    # ── Domain methods ─────────────────────────────────────────────────────────

    async def expirations(self, sym: str) -> List[str]:
        rows = await self._get("/option/list/expirations", {"symbol": sym})
        exps = set()
        for item in rows:
            val = item.get("expiration") if isinstance(item, dict) else item
            if val:
                exps.add(str(val).replace("-", ""))
        return sorted(exps)

    async def greeks(self, sym: str, exp: str) -> List[Dict]:
        """
        Returns parsed greeks rows.  Each row includes the contract fields AND
        all header metadata, so underlying_price is directly accessible as
        row["underlying_price"].
        """
        return await self._get(
            "/option/snapshot/greeks/all", {"symbol": sym, "expiration": exp}
        )

    @staticmethod
    def extract_spot_from_greeks(rows: List[Dict]) -> Optional[float]:
        """
        underlying_price is injected into every row from the bulk response header.
        Checks multiple field name variants for safety.
        """
        for field in ("underlying_price", "last_stock_price", "stock_price",
                      "underlying", "spot", "unadjusted_price"):
            for row in rows:
                val = row.get(field)
                if val:
                    try:
                        f = float(val)
                        if f > 0:
                            return f
                    except (TypeError, ValueError):
                        continue
        return None

    async def open_interest(self, sym: str, exp: str) -> List[Dict]:
        """Fetch most-recent OI, looking back up to 7 calendar days."""
        today = datetime.date.today()
        for days_back in range(7):
            date_str = (today - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
            rows = await self._get(
                "/option/history/open_interest",
                {"symbol": sym, "expiration": exp, "date": date_str},
            )
            if rows:
                log.debug("  OI found for exp=%s on %s", exp, date_str)
                return rows
        return []

    async def close(self):
        await self.http.aclose()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _clean_sym(sym: str) -> str:
    for prefix in ("AMEX:", "NASDAQ:", "NYSE:", "CBOE:", "CME:"):
        sym = sym.replace(prefix, "")
    return sym.upper().strip()


def _right(val) -> str:
    """Normalize right (call/put) to single char 'C' or 'P'."""
    if isinstance(val, str):
        return val.strip().upper()[0]
    return "C" if val == 0 else "P"


def _write_csv(path: Path, close_val: float) -> None:
    """
    Write (or overwrite) a single-row OHLCV CSV with the current UTC timestamp.
    Pine Seeds forward-fills the latest row to all subsequent chart bars.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        w.writerow([ts, close_val, close_val, close_val, close_val, 0])


def _find_flip(strikes_sorted: List[Tuple[float, float]], spot: float) -> float:
    """
    Linear interpolation between adjacent strikes that straddle the GEX zero-crossing.
    Falls back to the strike closest to spot if no crossing is found.
    """
    for i in range(len(strikes_sorted) - 1):
        s1, g1 = strikes_sorted[i]
        s2, g2 = strikes_sorted[i + 1]
        if g1 < 0 < g2:
            return s1 - g1 * (s2 - s1) / (g2 - g1)
    return min(strikes_sorted, key=lambda x: abs(x[0] - spot))[0]


def _compute_vanna(item: dict, spot: float, exp_date: datetime.date) -> Optional[float]:
    """
    Extract vanna from ThetaData if present; otherwise derive from Black-Scholes.

    vanna = ∂²V/∂S∂σ = -N'(d1) × d2 / σ

    Positive vanna (OTM calls): as IV rises, delta increases toward 0.5.
    Negative vanna (OTM puts):  as IV rises, delta decreases toward -0.5.

    Dealers are net short options, so their aggregate vanna is the negative of the
    open-interest-weighted sum computed here (buyer perspective).
    """
    # Try direct extraction first — ThetaData may return it in the greeks snapshot
    raw = item.get("vanna")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

    # Fall back to Black-Scholes derivation using IV
    try:
        iv = float(item.get("iv") or item.get("implied_volatility") or 0)
        if iv <= 0:
            return None
        K = float(item["strike"])
        today = datetime.date.today()
        T = max((exp_date - today).days / 365.0, 1 / 365.0)  # floor at 1 day for 0DTE
        r = 0.05  # approximate risk-free rate
        d1 = (math.log(spot / K) + (r + iv ** 2 / 2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        n_prime_d1 = math.exp(-d1 ** 2 / 2) / math.sqrt(2 * math.pi)
        return -n_prime_d1 * d2 / iv
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


# ─── GEX Calculation ───────────────────────────────────────────────────────────

async def build_gex_profile(client: ThetaClient, sym: str) -> Optional[Dict]:
    sym = _clean_sym(sym)

    log.info("[%s] Fetching expirations...", sym)
    all_exps  = await client.expirations(sym)
    today_str = datetime.date.today().strftime("%Y%m%d")
    future    = [e for e in all_exps if e >= today_str][:NUM_EXPS]
    if not future:
        log.error("[%s] No future expirations found", sym)
        return None
    log.info("[%s] Using expirations: %s", sym, future)

    # ── Spot price ────────────────────────────────────────────────────────────
    # Primary: extract from greeks snapshot — works with Options: PROFESSIONAL only.
    # Fallback: Stock/Index snapshot endpoint (requires paid Stock or Index tier).
    log.info("[%s] Fetching greeks for nearest expiration to derive spot...", sym)
    first_greeks = await client.greeks(sym, future[0])
    spot = ThetaClient.extract_spot_from_greeks(first_greeks)

    if not spot:
        log.warning("[%s] Spot not found in greeks payload — trying stock/index endpoint...", sym)
        spot = await client.spot_from_stock(sym)

    if not spot:
        if first_greeks:
            log.error(
                "[%s] Could not find underlying_price in greeks rows. "
                "First row keys: %s  |  First row values: %s",
                sym,
                list(first_greeks[0].keys()),
                {k: v for k, v in first_greeks[0].items()
                 if any(kw in k.lower() for kw in ("price", "under", "spot", "stock", "last"))},
            )
        else:
            log.error("[%s] Greeks snapshot returned no rows — check symbol and ThetaData Terminal.", sym)
        return None
    log.info("[%s] Spot = %.2f", sym, spot)

    gex_by_strike: Dict[float, float] = {}
    vex_by_strike: Dict[float, float] = {}

    for exp in future:
        log.info("[%s]   Processing %s...", sym, exp)
        # Reuse the already-fetched greeks for the first expiration
        greeks_raw = first_greeks if exp == future[0] else await client.greeks(sym, exp)
        oi_raw     = await client.open_interest(sym, exp)
        if not greeks_raw or not oi_raw:
            log.warning("[%s]   Skipping %s: missing greeks or OI", sym, exp)
            continue

        exp_date = datetime.datetime.strptime(exp, "%Y%m%d").date()

        # (strike, right) → gamma and vanna, parsed in a single pass
        gamma_map: Dict[Tuple, float] = {}
        vanna_map: Dict[Tuple, float] = {}
        for item in greeks_raw:
            try:
                key = (float(item["strike"]), _right(item["right"]))
                gamma_map[key] = float(item.get("gamma") or 0)
                v = _compute_vanna(item, spot, exp_date)
                if v is not None:
                    vanna_map[key] = v
            except (KeyError, TypeError, ValueError):
                continue

        # (strike, right) → max OI seen across rows
        oi_map: Dict[Tuple, float] = {}
        for item in oi_raw:
            try:
                key = (float(item["strike"]), _right(item["right"]))
                oi  = float(item.get("open_interest") or item.get("oi") or 0)
                if oi > oi_map.get(key, 0):
                    oi_map[key] = oi
            except (KeyError, TypeError, ValueError):
                continue

        lo, hi = spot * (1 - STRIKE_RANGE), spot * (1 + STRIKE_RANGE)
        for (strike, right), gamma in gamma_map.items():
            if not (lo <= strike <= hi):
                continue
            oi = oi_map.get((strike, right), 0.0)
            if oi == 0:
                continue

            # GEX: gamma × OI × 100 × spot; puts negate (dealer sells into weakness)
            gex = gamma * oi * 100 * spot
            if right == "P":
                gex = -gex
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex

            # VEX: vanna × OI × 100 (buyer perspective; sign comes from BS formula)
            vanna = vanna_map.get((strike, right))
            if vanna is not None:
                vex_by_strike[strike] = vex_by_strike.get(strike, 0.0) + vanna * oi * 100

    if not gex_by_strike:
        log.error("[%s] No GEX data computed across all expirations", sym)
        return None

    strikes_sorted  = sorted(gex_by_strike.items())
    total_abs_gex   = sum(abs(v) for v in gex_by_strike.values()) or 1.0

    def _sig(gex: float) -> float:
        return abs(gex) / total_abs_gex * 100

    # Top 3 call walls (highest positive GEX) and put walls (most negative GEX)
    pos_sorted  = sorted([(s, g) for s, g in gex_by_strike.items() if g > 0],
                         key=lambda x: x[1], reverse=True)
    neg_sorted  = sorted([(s, g) for s, g in gex_by_strike.items() if g < 0],
                         key=lambda x: x[1])
    call_walls  = [(s, g, _sig(g)) for s, g in pos_sorted[:3]]
    put_walls   = [(s, g, _sig(g)) for s, g in neg_sorted[:3]]

    return {
        "sym":           sym,
        "spot":          spot,
        "flip":          _find_flip(strikes_sorted, spot),
        # Primary walls (scalar) kept for backward compat
        "call_wall":     call_walls[0][0] if call_walls else 0.0,
        "put_wall":      put_walls[0][0]  if put_walls  else 0.0,
        # Top-3 walls with significance — [(strike, gex, sig_pct), ...]
        "call_walls":    call_walls,
        "put_walls":     put_walls,
        "total_abs_gex": total_abs_gex,
        # Regime scalars
        "total_net_gex": sum(gex_by_strike.values()),
        "total_net_vex": sum(vex_by_strike.values()),
        # Top N strikes by absolute GEX — histogram bars in Pine Script
        "top_levels":    sorted(
            gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True
        )[:TOP_N],
    }


# ─── CSV Output ────────────────────────────────────────────────────────────────

def write_seed_files(result: Dict) -> None:
    sym    = result["sym"]
    levels = result["top_levels"]   # [(strike, gex), ...]

    # Primary key levels
    _write_csv(DATA_DIR / f"{sym}_GEX_FLIP.csv",       result["flip"])
    _write_csv(DATA_DIR / f"{sym}_GEX_CALLWALL.csv",   result["call_wall"])
    _write_csv(DATA_DIR / f"{sym}_GEX_PUTWALL.csv",    result["put_wall"])

    # Regime scalars
    _write_csv(DATA_DIR / f"{sym}_GEX_NET_TOTAL.csv",  result["total_net_gex"])
    _write_csv(DATA_DIR / f"{sym}_VEX_NET_TOTAL.csv",  result["total_net_vex"])

    # Secondary and tertiary call/put walls
    for walls, prefix in (
        (result["call_walls"], "GEX_CALLWALL"),
        (result["put_walls"],  "GEX_PUTWALL"),
    ):
        for idx in (2, 3):
            entry  = walls[idx - 1] if len(walls) >= idx else None
            strike = entry[0] if entry else 0.0
            _write_csv(DATA_DIR / f"{sym}_{prefix}_{idx:02d}.csv", strike)

    # Ranked histogram bars — strike price and GEX value stored as separate series
    for i in range(TOP_N):
        idx           = i + 1
        strike, gex   = levels[i] if i < len(levels) else (0.0, 0.0)
        _write_csv(DATA_DIR / f"{sym}_STRIKE_{idx:02d}.csv",  strike)
        _write_csv(DATA_DIR / f"{sym}_GEX_VAL_{idx:02d}.csv", gex)

    total_files = 3 + 2 + 4 + TOP_N * 2   # flip/walls + net scalars + secondary walls + histogram
    log.info("[%s] Wrote %d seed CSV files to %s/", sym, total_files, DATA_DIR)


# ─── Summary ───────────────────────────────────────────────────────────────────

def _print_summary(results: List[Dict]) -> None:
    sep  = "═" * 62
    div  = "─" * 62
    now  = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [sep, f"  GEX SUMMARY — {now}", sep]

    for i, r in enumerate(results):
        if i:
            lines.append(div)
        spot       = r["spot"]
        flip       = r["flip"]
        flip_dist  = (flip - spot) / spot * 100

        lines.append(f"  {r['sym']}  spot={spot:.2f}  flip={flip:.2f} ({flip_dist:+.2f}% from spot)")

        def _wall_str(walls: list) -> str:
            parts = []
            for strike, _gex, sig in walls:
                parts.append(f"{strike:.1f} ({sig:.0f}%)")
            return "  ".join(parts) if parts else "—"

        lines.append(f"    Call Walls:  {_wall_str(r['call_walls'])}")
        lines.append(f"    Put  Walls:  {_wall_str(r['put_walls'])}")

        gex_pos = r["total_net_gex"] >= 0
        vex_pos = r["total_net_vex"] >= 0
        lines.append(
            f"    Net GEX:     {'POSITIVE — price-dampening' if gex_pos else 'NEGATIVE — price-amplifying'}"
            f"  ({r['total_net_gex']:.3e})"
        )
        lines.append(
            f"    Net VEX:     {'IV↓ → dealer BUY pressure' if vex_pos else 'IV↓ → dealer SELL pressure'}"
            f"  ({r['total_net_vex']:.3e})"
        )

    lines.append(sep)
    for line in lines:
        log.info(line)


# ─── Entry Point ───────────────────────────────────────────────────────────────

async def process_sym(client: ThetaClient, sym: str) -> Optional[Dict]:
    result = await build_gex_profile(client, sym)
    if result:
        write_seed_files(result)
        log.info(
            "[%s] Complete — spot=%.2f flip=%.2f call_wall=%.2f put_wall=%.2f",
            result["sym"], result["spot"],
            result["flip"], result["call_wall"], result["put_wall"],
        )
    return result


async def main(symbols: List[str]) -> None:
    client = ThetaClient()
    try:
        results = await asyncio.gather(*[process_sym(client, sym) for sym in symbols])
        completed = [r for r in results if r]
        if completed:
            _print_summary(completed)
    finally:
        await client.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GEX Profile Pine Seeds Generator")
    parser.add_argument("symbols", nargs="*", help="Symbols to process (default: DEFAULT_SYMS)")
    parser.add_argument("--exps", type=int, default=NUM_EXPS,
                        help=f"Number of nearest expirations to aggregate (default: {NUM_EXPS})")
    args = parser.parse_args()

    syms = [s.upper() for s in args.symbols] if args.symbols else DEFAULT_SYMS
    NUM_EXPS = args.exps
    asyncio.run(main(syms))
