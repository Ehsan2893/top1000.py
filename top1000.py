# Paper-trading simulator (NO real money) - top 1000 coins by market cap
# Runs 4 strategy variants side by side on the same signals:
#   spot_fixed, spot_trail, lev3_fixed, lev3_trail (3x leverage, long only)
# One run per execution (GitHub Actions). Data: CoinGecko free API.
import requests, time, json, os, csv
from datetime import datetime

URL = "https://api.coingecko.com/api/v3/coins/markets"
STATE = "sim_state.json"
SNAP = "sim_snap.json"
LOG = "sim_trades.csv"

CFG = dict(
    start_balance=40.0,   # fake dollars per variant
    margin=8.0,           # fake dollars of own money per trade
    max_open=5,
    max_minutes=60,
    cost_pct=0.4,         # fees+slippage per round trip, % of position size
    funding_hr_pct=0.0015,  # leveraged funding cost per hour, % of position size
    maint_pct=0.5,        # maintenance margin used for liquidation
    min_vol=5000000,
    min_mcap=50000000,
    min_h1=1.0,
    max_h1=4.0,
    min_m5=0.3,
    max_d1=15.0,
    cooldown_h=3,
    pages=4,              # 4 pages x 250 = 1000 coins
)

# lev: leverage | tp: take profit % (price move) | sl: stop loss % | trail: trailing stop %
VARIANTS = {
    "spot_fixed": dict(lev=1, tp=1.5, sl=-1.0, trail=None),
    "spot_trail": dict(lev=1, tp=None, sl=-1.0, trail=1.0),
    "lev3_fixed": dict(lev=3, tp=1.5, sl=-1.0, trail=None),
    "lev3_trail": dict(lev=3, tp=None, sl=-1.0, trail=1.0),
}


def fetch_market():
    coins = []
    for page in range(1, CFG["pages"] + 1):
        for attempt in range(3):
            try:
                r = requests.get(URL, params={
                    "vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 250, "page": page, "sparkline": "false",
                    "price_change_percentage": "1h"},
                    headers={"User-Agent": "paperbot"}, timeout=25)
                if r.status_code == 200:
                    coins += r.json()
                    break
                print("HTTP", r.status_code, "page", page)
                if r.status_code == 429:
                    time.sleep(25)
            except Exception as e:
                print("net error:", str(e)[:80])
                time.sleep(5)
        time.sleep(2)
    return coins


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def log_trade(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "variant", "symbol", "entry", "exit",
                        "price_pct", "roi_pct", "profit_usd", "reason",
                        "minutes"])
        w.writerow(row)


def is_junk(c):
    sid = (c.get("id") or "").lower()
    sym = (c.get("symbol") or "").lower()
    price = c.get("current_price") or 0
    d1 = c.get("price_change_percentage_24h") or 0
    if "usd" in sid or "usd" in sym:
        return True
    if sid.startswith(("wrapped", "bridged", "staked", "binance-peg")):
        return True
    if "wbtc" in sid or "steth" in sid:
        return True
    return 0.97 <= price <= 1.03 and abs(d1) < 1.0


def liq_level(lev):
    return -(100.0 / lev - CFG["maint_pct"])


def close_pos(name, v, pos, price, reason, mins):
    lev = VARIANTS[name]["lev"]
    margin = CFG["margin"]
    notional = margin * lev
    raw = (price / pos["entry"] - 1) * 100
    cost = CFG["cost_pct"] + (CFG["funding_hr_pct"] * mins / 60 if lev > 1 else 0)
    if lev > 1 and raw <= liq_level(lev):
        pnl, reason = -margin, "LIQUIDATED"
    else:
        pnl = max(notional * (raw - cost) / 100, -margin)
    v["balance"] += margin + pnl
    log_trade([datetime.now().strftime("%m-%d %H:%M"), name, pos["sym"],
               pos["entry"], price, round(raw, 2), round(pnl / margin * 100, 1),
               round(pnl, 3), reason, round(mins)])
    print("CLOSE %s %s price %+.2f%% pnl $%.2f (%s)" % (
        name, pos["sym"], raw, pnl, reason))


def manage_open(name, v, prices):
    cfg = VARIANTS[name]
    still = []
    now = time.time()
    for pos in v["open"]:
        price = prices.get(pos["id"])
        mins = (now - pos["t"]) / 60
        if not price:
            if mins > CFG["max_minutes"] * 2:
                close_pos(name, v, pos, pos["entry"], "NO_DATA", mins)
            else:
                still.append(pos)
            continue
        pos["peak"] = max(pos.get("peak", pos["entry"]), price)
        raw = (price / pos["entry"] - 1) * 100
        base_stop = pos["entry"] * (1 + cfg["sl"] / 100)
        stop = base_stop
        if cfg["trail"]:
            stop = max(stop, pos["peak"] * (1 - cfg["trail"] / 100))
        reason = None
        if cfg["lev"] > 1 and raw <= liq_level(cfg["lev"]):
            reason = "LIQUIDATED"
        elif price <= stop:
            reason = "TRAIL" if stop > base_stop else "SL"
        elif cfg["tp"] and raw >= cfg["tp"]:
            reason = "TP"
        elif mins >= CFG["max_minutes"]:
            reason = "TIME"
        if reason:
            close_pos(name, v, pos, price, reason, mins)
        else:
            still.append(pos)
    v["open"] = still


def candidates(coins, snap):
    cands = []
    for c in coins:
        price = c.get("current_price") or 0
        h1 = c.get("price_change_percentage_1h_in_currency")
        d1 = c.get("price_change_percentage_24h") or 0
        prev = snap.get(c.get("id"))
        if price <= 0 or h1 is None or not prev or is_junk(c):
            continue
        m5 = (price / prev - 1) * 100
        if ((c.get("total_volume") or 0) >= CFG["min_vol"]
                and (c.get("market_cap") or 0) >= CFG["min_mcap"]
                and CFG["min_h1"] <= h1 <= CFG["max_h1"]
                and m5 >= CFG["min_m5"] and d1 <= CFG["max_d1"]):
            cands.append((m5, c))
    cands.sort(key=lambda x: -x[0])
    return cands


def open_entries(name, v, cands):
    now = time.time()
    v["recent"] = {k: t for k, t in v["recent"].items()
                   if now - t < CFG["cooldown_h"] * 3600}
    open_ids = set(o["id"] for o in v["open"])
    for m5, c in cands:
        if len(v["open"]) >= CFG["max_open"] or v["balance"] < CFG["margin"]:
            break
        if c["id"] in v["recent"] or c["id"] in open_ids:
            continue
        v["balance"] -= CFG["margin"]
        v["recent"][c["id"]] = now
        v["open"].append({"id": c["id"], "sym": c["symbol"].upper(),
                          "entry": c["current_price"], "t": now,
                          "peak": c["current_price"]})
        print("OPEN  %s %s @ %s (5m %+.2f%%)" % (
            name, c["symbol"].upper(), c["current_price"], m5))


def stats(name):
    if not os.path.exists(LOG):
        return "no closed trades yet"
    rows = [r for r in list(csv.reader(open(LOG)))[1:] if r[1] == name]
    n = len(rows)
    if n == 0:
        return "no closed trades yet"
    wins = sum(1 for r in rows if float(r[7]) > 0)
    total = sum(float(r[7]) for r in rows)
    return "trades=%d wins=%d (%.0f%%) P/L=$%.2f" % (
        n, wins, 100 * wins / n, total)


def main():
    s = load(STATE, {})
    for name in VARIANTS:
        s.setdefault(name, {"balance": CFG["start_balance"],
                            "open": [], "recent": {}})
    snap = load(SNAP, {})
    coins = fetch_market()
    if len(coins) < 200:
        print("not enough data, skipping this run")
        return
    prices = {c["id"]: c["current_price"] for c in coins if c.get("current_price")}
    cands = candidates(coins, snap)
    for name in VARIANTS:
        manage_open(name, s[name], prices)
        open_entries(name, s[name], cands)
    new_snap = {}
    for c in coins:
        h1 = c.get("price_change_percentage_1h_in_currency")
        if c.get("current_price") and h1 is not None and h1 >= 0.3:
            new_snap[c["id"]] = c["current_price"]
    save(SNAP, new_snap)
    save(STATE, s)
    for name in VARIANTS:
        print("%s: cash=$%.2f open=%d | %s" % (
            name, s[name]["balance"], len(s[name]["open"]), stats(name)))


if __name__ == "__main__":
    main()
