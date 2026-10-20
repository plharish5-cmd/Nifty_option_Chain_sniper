#!/usr/bin/env python3
"""Trade-log analyzer: pandas computes every number, the LLM only comments."""
import argparse, glob, json, os, sys
import pandas as pd
from openai import OpenAI

BASE_URL = os.environ.get("FREELLMAPI_URL", "http://localhost:3001/v1")
API_KEY = os.environ.get("FREELLMAPI_KEY",
                         "freellmapi-9648d07db1e66eaca78226512eee95efafc9e9092afd5769")


def r(x):
    return None if pd.isna(x) else round(float(x), 2)


def load(paths):
    files = []
    for p in paths:
        files += glob.glob(os.path.join(p, "*.csv")) if os.path.isdir(p) else glob.glob(p)
    files = sorted(set(files))
    if not files:
        sys.exit("No CSV files found.")
    frames = []
    for f in files:
        try:
            d = pd.read_csv(f)
        except pd.errors.EmptyDataError:
            continue
        if not d.empty:
            frames.append(d)
    if not frames:
        sys.exit("All files were empty.")
    df = pd.concat(frames, ignore_index=True)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df["exit_ts"] = pd.to_datetime(df["exit_ts"])
    for c in ["entry", "stop0", "exit", "points", "trails"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["points"]).sort_values("entry_ts").reset_index(drop=True)
    df["hold_min"] = (df["exit_ts"] - df["entry_ts"]).dt.total_seconds() / 60
    df["risk_pts"] = (df["entry"] - df["stop0"]).abs()
    df["r_multiple"] = df["points"] / df["risk_pts"].replace(0, float("nan"))
    df["entry_hour"] = df["entry_ts"].dt.hour
    return df, len(files)


def max_streak(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def group_stats(df, col):
    g = df.groupby(col)["points"]
    out = pd.DataFrame({
        "trades": g.size(),
        "win_rate_pct": g.apply(lambda s: (s > 0).mean() * 100),
        "total_pts": g.sum(),
        "avg_pts": g.mean(),
    }).round(2)
    return out


def summarize(df, n_files):
    p = df["points"]
    wins, losses = p[p > 0], p[p < 0]
    equity = p.cumsum()
    dd = equity - equity.cummax().clip(lower=0)
    daily = df.groupby("day")["points"].sum()
    loss_sum = abs(losses.sum())

    stats = {
        "files_read": n_files,
        "trading_days": int(daily.size),
        "trades": int(len(p)),
        "wins": int(len(wins)), "losses": int(len(losses)),
        "scratch": int((p == 0).sum()),
        "win_rate_pct": r((p > 0).mean() * 100),
        "total_points": r(p.sum()),
        "avg_points_per_trade": r(p.mean()),
        "avg_win": r(wins.mean()), "avg_loss": r(losses.mean()),
        "largest_win": r(p.max()), "largest_loss": r(p.min()),
        "profit_factor": r(wins.sum() / loss_sum) if loss_sum else None,
        "avg_r_multiple": r(df["r_multiple"].mean()),
        "avg_initial_risk_pts": r(df["risk_pts"].mean()),
        "max_drawdown_pts": r(dd.min()),
        "max_win_streak": max_streak(p > 0),
        "max_loss_streak": max_streak(p < 0),
        "avg_hold_min": r(df["hold_min"].mean()),
        "green_days": int((daily > 0).sum()), "red_days": int((daily < 0).sum()),
        "best_day_pts": r(daily.max()), "worst_day_pts": r(daily.min()),
    }
    if stats["trades"] < 30:
        stats["sample_warning"] = f"Only {stats['trades']} trades - too few for conclusions."

    tables = {c: group_stats(df, c) for c in
              ["direction", "reason", "trigger", "entry_hour", "trails", "source"]
              if c in df.columns}
    return stats, tables


def comment(stats, tables, model):
    payload = {"overall": stats,
               "breakdowns": {k: json.loads(v.to_json(orient="index")) for k, v in tables.items()}}
    system = (
        "You review an intraday index strategy's trade log (points are already direction-adjusted). "
        "All numbers in the JSON were computed in Python and are correct. Do NOT calculate, estimate "
        "or invent any new numbers or price levels; only quote figures present in the JSON. "
        "No buy/sell signals or market predictions. Cover: 1) whether the stats show an edge, "
        "2) weak spots by direction, exit reason, entry hour, trails, 3) up to 3 concrete things "
        "to test next in a backtest. Say plainly if the sample is too small. Under 250 words."
    )
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    raw = client.chat.completions.with_raw_response.create(
        model=model, temperature=0.3,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(payload, indent=1)}],
    )
    resp = raw.parse()
    print(f"\n=== LLM commentary (routed via: {raw.headers.get('X-Routed-Via', '?')}, "
          f"model: {resp.model}) ===\n")
    print(resp.choices[0].message.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=["./data/strategy2_log"],
                    help="CSV files, globs or folders (default: ./data/strategy2_log)")
    ap.add_argument("--model", default="auto")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    df, n = load(a.paths)
    stats, tables = summarize(df, n)

    print("=== Overall (computed in pandas) ===")
    for k, v in stats.items():
        print(f"{k:24} {v}")
    for name, t in tables.items():
        print(f"\n--- by {name} ---\n{t.to_string()}")

    if not a.no_llm:
        try:
            comment(stats, tables, a.model)
        except Exception as e:
            print(f"\nLLM call failed: {e}")


if __name__ == "__main__":
    main()
