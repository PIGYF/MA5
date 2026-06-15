from __future__ import annotations

import argparse
import csv
import html
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from backtest import Bar, fetch_bars, rolling_sma, rolling_sum, score_signal_strength


DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO", "AMD",
    "NFLX", "PLTR", "COIN", "MSTR", "SMCI", "ARM", "CRWD", "NET", "DDOG", "SNOW",
    "SHOP", "SQ", "HOOD", "RBLX", "UBER", "ABNB", "DASH", "APP", "SOFI", "ROKU",
    "ASTS", "RKLB", "IONQ", "OKLO", "VRT", "DELL", "MU", "MRVL", "TSM", "ASML",
    "QCOM", "AMAT", "LRCX", "KLAC", "PANW", "ZS", "MDB", "TEAM", "NOW", "ORCL",
    "CRM", "ADBE", "INTC", "IBM", "TXN", "ADI", "NIO", "LI", "XPEV", "BABA",
    "PDD", "JD", "SE", "MELI", "CELH", "ELF", "CAVA", "HIMS", "WING", "CMG",
    "LLY", "NVO", "ISRG", "VRTX", "REGN", "VRTX", "MRNA", "CRSP", "BEAM", "RXRX",
    "QQQ", "SPY", "IWM", "TQQQ", "SOXL", "ARKK",
]


@dataclass
class SignalResult:
    symbol: str
    signal_date: str
    close: float
    ma: float
    dist_to_ma_pct: float
    volume: float
    vol_ma: float
    volume_ratio: float
    massive_count_7d: int
    signal_type: str
    avg_dollar_volume_20d: float
    technical_score: float = 0.0
    technical_rating: str = ""
    technical_notes: str = ""
    company_name: str = ""
    market_cap: float = 0.0
    country: str = ""
    sector: str = ""
    industry: str = ""
    asset_type: str = ""
    second_stage_rating: str = ""
    second_stage_score_total: int = 0
    catalyst_label: str = ""
    catalyst_score: int = 0
    catalyst_yahoo_url: str = ""
    catalyst_google_url: str = ""
    sector_label: str = ""
    sector_score: int = 0
    sector_peer_count: int = 0
    industry_peer_count: int = 0
    space_label: str = ""
    space_score: int = 0
    distance_52w_high_pct: float = 0.0
    above_200ma: str = ""
    distance_200ma_pct: float = 0.0
    nearest_resistance_pct: float = 0.0
    candle_label: str = ""
    candle_score: int = 0
    day_change_pct: float = 0.0
    close_position_pct: float = 0.0
    upper_shadow_body_ratio: float = 0.0
    next_earnings_date: str = ""
    earnings_days: int = 9999
    earnings_status: str = ""
    ma5_rising: bool = False
    ma5_gt_20: bool = False
    ma20_gt_50: bool = False
    big_red_b1: bool = False
    above_ma5_3d: bool = False
    secondary_tags: str = ""


def unique_symbols(symbols: list[str]) -> list[str]:
    seen = set()
    result = []
    for symbol in symbols:
        clean = symbol.strip().upper()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def load_symbols(path: Path | None) -> list[str]:
    if path is None:
        return unique_symbols(DEFAULT_SYMBOLS)
    text = path.read_text(encoding="utf-8-sig")
    symbols = []
    for line in text.splitlines():
        if not line.strip():
            continue
        symbols.append(line.split(",")[0])
    return unique_symbols(symbols)


def technical_strength_for_latest_signal(
    bars: list[Bar],
    ma_values: list[float | None],
    vol_ma_values: list[float | None],
    signal_type: str,
) -> dict[str, float | str]:
    index = len(bars) - 1
    equity_curve: list[dict[str, float | str]] = []
    for i, bar in enumerate(bars):
        equity_curve.append(
            {
                "date": bar.date,
                "ma": "" if ma_values[i] is None else ma_values[i],
                "vol_ma": "" if vol_ma_values[i] is None else vol_ma_values[i],
                "dynamic_stop": "",
            }
        )
    return score_signal_strength(bars, equity_curve, index, signal_type)


def latest_b_signal(
    symbol: str,
    bars: list[Bar],
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
    reentry_pct: float,
    min_price: float,
    min_avg_dollar_volume: float,
    vol_high_days: int = 3,
    vol_high_multiplier: float = 1.0,
    massive_window: int = 7,
    massive_min_count: int = 1,
    massive_max_count: int = 2,
    b1_require_20ma_gt_50ma: bool = False,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> SignalResult | None:
    result, _ = latest_b_signal_with_reason(
        symbol,
        bars,
        ma_length,
        vol_length,
        vol_multiplier,
        reentry_pct,
        min_price,
        min_avg_dollar_volume,
        vol_high_days,
        vol_high_multiplier,
        massive_window,
        massive_min_count,
        massive_max_count,
        b1_require_20ma_gt_50ma,
        require_ma5_rising,
        require_5ma_gt_20ma,
    )
    return result


def latest_b_signal_with_reason(
    symbol: str,
    bars: list[Bar],
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
    reentry_pct: float,
    min_price: float,
    min_avg_dollar_volume: float,
    vol_high_days: int = 3,
    vol_high_multiplier: float = 1.0,
    massive_window: int = 7,
    massive_min_count: int = 1,
    massive_max_count: int = 2,
    b1_require_20ma_gt_50ma: bool = False,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> tuple[SignalResult | None, str]:
    if len(bars) < max(ma_length, vol_length) + 10:
        return None, f"数据不足：需要至少 {max(ma_length, vol_length) + 10} 根日K，当前 {len(bars)} 根"

    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma = rolling_sma(closes, ma_length)
    ma20 = rolling_sma(closes, 20)
    ma50 = rolling_sma(closes, 50)
    vol_ma = rolling_sma(volumes, vol_length)
    is_vol_high = [
        vol_ma[i] is not None and bars[i].volume > vol_ma[i] * vol_high_multiplier for i in range(len(bars))
    ]
    is_massive_vol = [
        vol_ma[i] is not None and bars[i].volume >= vol_ma[i] * vol_multiplier
        for i in range(len(bars))
    ]
    massive_counts = rolling_sum([1 if value else 0 for value in is_massive_vol], massive_window)

    i = len(bars) - 1
    bar = bars[i]
    if bar.close < min_price:
        return None, f"价格过滤：最新收盘 {bar.close:.2f} 低于最低价格 {min_price:.2f}"
    if ma[i] is None or vol_ma[i] is None or massive_counts[i] is None:
        return None, "指标数据不足：均线/均量/7日巨量窗口尚未形成"

    avg_dollar_volume = sum(b.close * b.volume for b in bars[-20:]) / min(20, len(bars))
    if avg_dollar_volume < min_avg_dollar_volume:
        return None, f"成交额过滤：20日均成交额 {avg_dollar_volume / 1_000_000:.1f}M 低于阈值 {min_avg_dollar_volume / 1_000_000:.1f}M"

    ma_is_rising = ma[i - 1] is not None and ma[i] > ma[i - 1]
    vol_days_high = i >= vol_high_days - 1 and all(is_vol_high[j] for j in range(i - vol_high_days + 1, i + 1))
    has_massive_vol = massive_counts[i] >= massive_min_count
    price_above_ma = bar.close > ma[i]
    ma5_gt_20 = ma[i] is not None and ma20[i] is not None and ma[i] > ma20[i]
    ma20_gt_50 = ma20[i] is not None and ma50[i] is not None and ma20[i] > ma50[i]

    def bull_quality_ok_at(index: int) -> bool:
        recent_high = max(closes[max(0, index - 59) : index + 1])
        ma50_rising = ma50[index] is not None and index >= 5 and ma50[index - 5] is not None and ma50[index] > ma50[index - 5]
        above_ma50 = ma50[index] is not None and bars[index].close > ma50[index]
        near_stage_high = recent_high > 0 and bars[index].close >= recent_high * 0.80
        return bool(above_ma50 and ma50_rising and near_stage_high)

    bull_quality_ok = bull_quality_ok_at(i)
    trend_confirmed = False
    for j in range(i):
        if ma[j] is None or massive_counts[j] is None:
            continue
        j_ma_rising = j > 0 and ma[j - 1] is not None and ma[j] > ma[j - 1]
        j_vol_days_high = j >= vol_high_days - 1 and all(is_vol_high[k] for k in range(j - vol_high_days + 1, j + 1))
        j_has_massive_vol = massive_counts[j] >= massive_min_count
        j_price_above_ma = bars[j].close > ma[j]
        j_ma5_gt_20 = ma[j] is not None and ma20[j] is not None and ma[j] > ma20[j]
        j_ma20_gt_50 = ma20[j] is not None and ma50[j] is not None and ma20[j] > ma50[j]
        j_initial_buy = (
            j_price_above_ma
            and j_vol_days_high
            and j_has_massive_vol
            and (j_ma_rising or not require_ma5_rising)
            and (j_ma20_gt_50 or not b1_require_20ma_gt_50ma)
            and (j_ma5_gt_20 or not require_5ma_gt_20ma)
        )
        if j_initial_buy:
            trend_confirmed = True
        if bars[j].close < ma[j] * 0.925:
            trend_confirmed = False

    dist_to_ma = abs(bar.close - ma[i]) / ma[i]
    initial_buy = (
        price_above_ma
        and vol_days_high
        and has_massive_vol
        and (ma_is_rising or not require_ma5_rising)
        and (ma20_gt_50 or not b1_require_20ma_gt_50ma)
        and (ma5_gt_20 or not require_5ma_gt_20ma)
    )
    full_range = bar.high - bar.low
    body = abs(bar.close - bar.open)
    upper_shadow = bar.high - max(bar.open, bar.close)
    close_position = (bar.close - bar.low) / full_range if full_range > 0 else 0.5
    upper_shadow_ok = upper_shadow <= max(body * 0.75, full_range * 0.20)
    reentry_buy = (
        trend_confirmed
        and is_massive_vol[i]
        and price_above_ma
        and dist_to_ma <= reentry_pct / 100
        and bar.close > bar.open
        and (ma_is_rising or not require_ma5_rising)
        and (ma5_gt_20 or not require_5ma_gt_20ma)
        and bull_quality_ok
        and upper_shadow_ok
    )
    if not (initial_buy or reentry_buy):
        failed = []
        if not price_above_ma:
            failed.append("收盘价未站上MA")
        if require_ma5_rising and not ma_is_rising:
            failed.append("MA未向上")
        if not vol_days_high:
            failed.append("未连续3天放量")
        if not has_massive_vol:
            failed.append(f"7日巨量次数为 {int(massive_counts[i])}，不在1到2次")
        if b1_require_20ma_gt_50ma and not ma20_gt_50:
            failed.append("B1 20MA>50MA 趋势过滤未通过")
        if require_5ma_gt_20ma and not ma5_gt_20:
            failed.append("5MA 未大于 20MA")
        if not is_massive_vol[i]:
            failed.append("当日不是巨量")
        if dist_to_ma > reentry_pct / 100:
            failed.append(f"距MA {dist_to_ma * 100:.2f}% 超过反抽距离 {reentry_pct:.2f}%")
        if bar.close <= bar.open:
            failed.append("当日不是阳线")
        return None, "未出现B点：" + "；".join(failed)

    if reentry_buy:
        signal_type = "B2_reentry"
    elif initial_buy:
        signal_type = "B1_trend_confirm"
    else:
        signal_type = "B"
    strength = technical_strength_for_latest_signal(bars, ma, vol_ma, "B")
    previous_close = bars[i - 1].close if i > 0 else bar.close
    body_pct = (bar.open - bar.close) / previous_close * 100 if previous_close else 0.0
    big_red_b1 = (
        signal_type == "B1_trend_confirm"
        and bar.close < bar.open
        and (body_pct >= 2.5 or (body_pct >= 1.8 and close_position <= 0.35))
    )
    above_ma5_3d = i >= 2 and all(ma[j] is not None and bars[j].close > ma[j] for j in range(i - 2, i + 1))
    secondary_tags = " / ".join(
        tag
        for tag, enabled in (
            ("big_red_b1", big_red_b1),
            ("above_ma5_3d", above_ma5_3d),
        )
        if enabled
    )

    return SignalResult(
        symbol=symbol,
        signal_date=bar.date,
        close=bar.close,
        ma=ma[i],
        dist_to_ma_pct=dist_to_ma * 100,
        volume=bar.volume,
        vol_ma=vol_ma[i],
        volume_ratio=bar.volume / vol_ma[i],
        massive_count_7d=int(massive_counts[i]),
        signal_type=signal_type,
        avg_dollar_volume_20d=avg_dollar_volume,
        technical_score=float(strength["signal_score"]),
        technical_rating=str(strength["signal_rating"]),
        technical_notes=str(strength["score_notes"]),
        ma5_rising=bool(ma_is_rising),
        ma5_gt_20=bool(ma5_gt_20),
        ma20_gt_50=bool(ma20_gt_50),
        big_red_b1=big_red_b1,
        above_ma5_3d=above_ma5_3d,
        secondary_tags=secondary_tags,
    ), "符合B点"


def write_html(path: Path, rows: list[SignalResult], end: str) -> None:
    table_rows = "\n".join(
        f"<tr><td>{html.escape(r.symbol)}</td><td>{html.escape(r.company_name or '-')}</td><td>{r.market_cap / 1_000_000_000:.2f}B</td>"
        f"<td>{html.escape(r.country or '-')}</td><td>{html.escape(r.sector or '-')}</td><td>{html.escape(r.industry or '-')}</td><td>{html.escape(r.asset_type or '-')}</td>"
        f"<td>{html.escape(r.next_earnings_date or 'Unknown')}</td><td>{'' if r.earnings_days == 9999 else r.earnings_days}</td><td>{html.escape(r.earnings_status or '-')}</td>"
        f"<td>{html.escape(r.signal_date)}</td><td>{html.escape(r.signal_type)}</td>"
        f"<td>{r.technical_score:.1f}</td><td>{html.escape(r.technical_rating or '-')}</td>"
        f"<td>{r.close:.2f}</td><td>{r.ma:.2f}</td><td>{r.dist_to_ma_pct:.2f}%</td>"
        f"<td>{r.volume_ratio:.2f}x</td><td>{r.massive_count_7d}</td>"
        f"<td>{r.avg_dollar_volume_20d / 1_000_000:.1f}M</td></tr>"
        for r in rows
    )
    if not table_rows:
        table_rows = '<tr><td colspan="20" class="empty">No candidates found.</td></tr>'
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Next B Screener</title>
<style>
body {{ font-family: Arial, "Microsoft YaHei", sans-serif; background: #f4f6f8; color: #1f2933; margin: 0; padding: 24px; }}
main {{ max-width: 1360px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin: 0 0 8px; }}
p {{ color: #607080; }}
table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dde3ea; border-radius: 8px; overflow: hidden; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: right; font-size: 13px; }}
th {{ background: #f8fafc; color: #475569; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6), th:nth-child(7), td:nth-child(7) {{ text-align: left; }}
.empty {{ text-align: center; color: #607080; }}
</style></head><body><main>
<h1>下一交易日 B 点候选</h1>
<p>筛选口径：最新已完成日 K 在 {end} 附近出现 B 信号，因此下一交易日开盘才是策略买入点。</p>
<table><thead><tr><th>Symbol</th><th>Company</th><th>Mkt Cap</th><th>Country</th><th>Sector</th><th>Industry</th><th>Asset</th><th>Next Earnings</th><th>Days</th><th>Status</th><th>Signal Date</th><th>Signal</th><th>Tech</th><th>Tech Rating</th><th>Close</th><th>MA</th><th>Dist</th><th>Vol Ratio</th><th>Massive 7D</th><th>20D $Vol</th></tr></thead>
<tbody>{table_rows}</tbody></table>
</main></body></html>""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描最新日 K 出现 B 信号、下一交易日可执行的美股")
    parser.add_argument("--universe", type=Path, help="股票池 CSV/TXT，每行第一个字段为代码")
    parser.add_argument("--symbols", help="逗号分隔股票代码，会覆盖默认股票池")
    parser.add_argument("--start", default=(date.today() - timedelta(days=420)).isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--ma-length", type=int, default=5)
    parser.add_argument("--vol-length", type=int, default=20)
    parser.add_argument("--vol-multiplier", type=float, default=1.45)
    parser.add_argument("--reentry-pct", type=float, default=4.5)
    parser.add_argument("--min-price", type=float, default=5)
    parser.add_argument("--min-avg-dollar-volume", type=float, default=20_000_000)
    parser.add_argument("--csv-out", type=Path, default=Path("next_b_candidates.csv"))
    parser.add_argument("--html-out", type=Path, default=Path("next_b_candidates.html"))
    args = parser.parse_args()

    symbols = unique_symbols(args.symbols.split(",")) if args.symbols else load_symbols(args.universe)
    rows = []
    errors = []
    for symbol in symbols:
        try:
            bars = fetch_bars("yfinance", symbol, args.start, args.end, "qfq", None)
            result = latest_b_signal(
                symbol,
                bars,
                args.ma_length,
                args.vol_length,
                args.vol_multiplier,
                args.reentry_pct,
                args.min_price,
                args.min_avg_dollar_volume,
            )
            if result:
                rows.append(result)
        except Exception as exc:
            errors.append((symbol, str(exc)))

    rows.sort(key=lambda row: row.avg_dollar_volume_20d, reverse=True)
    with args.csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SignalResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    write_html(args.html_out, rows, args.end)

    print(f"Scanned: {len(symbols)}")
    print(f"Candidates: {len(rows)}")
    print(f"Errors: {len(errors)}")
    for row in rows:
        print(
            f"{row.symbol}: {row.signal_date} {row.signal_type}, "
            f"close={row.close:.2f}, vol={row.volume_ratio:.2f}x, dist={row.dist_to_ma_pct:.2f}%"
        )
    print(f"CSV: {args.csv_out.resolve()}")
    print(f"HTML: {args.html_out.resolve()}")


if __name__ == "__main__":
    main()
