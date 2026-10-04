"""Render the README backtest chart for every README language and GitHub theme.

Reads the continuous all-months backtest (one 50,000 RUB account carried from
2025-10-01 to 2026-10-02) and IMOEX daily closes from the fresh-tail market
cache, then writes docs/assets/backtest/equity-<language>-<theme>.svg.
Works offline: no broker, token or network access.

    python -m scripts.render_backtest_chart
"""
import argparse
import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from argonus.paths import BACKTEST_DIR, PROJECT_ROOT

BACKTEST = BACKTEST_DIR / "opening_integration_2026-10-03/all_months.json"
INDEX_CACHE = BACKTEST_DIR / "profit_first_2026-10-03/fresh_tail/market_cache.json.gz"
OUTPUT = PROJECT_ROOT / "docs/assets/backtest"
# The 07:05 entry rules were selected on data through 2026-07-16.
FRESH_FROM = date(2026, 7, 17)

THEMES = {
    "light": {"surface": "#ffffff", "primary": "#0b0b0b", "secondary": "#52514e",
              "grid": "#e1e0d9", "baseline": "#c3c2b7", "band": "#f0efec",
              "account": "#2a78d6", "index": "#898781"},
    "dark": {"surface": "#0d1117", "primary": "#ffffff", "secondary": "#c3c2b7",
             "grid": "#2c2c2a", "baseline": "#383835", "band": "#161b22",
             "account": "#3987e5", "index": "#898781"},
}

CJK_MONTHS = [f"{month}月" for month in range(1, 13)]
LANGUAGES = {
    "ru": {"group": "\u00a0", "point": ",", "percent": "%",
           "months": "янв фев мар апр май июн июл авг сен окт ноя дек".split(),
           "year": "{}", "day": "{d:02d}.{m:02d}.{y}",
           "title": "Бектест Argonus на счёте {start}",
           "subtitle": "{first} – {last} · сделок: {trades} · комиссии и проскальзывание учтены",
           "account": "Счёт Argonus", "index": "IMOEX на те же {start}",
           "fresh": "новые данные", "drawdown": "Просадка от максимума"},
    "en": {"group": ",", "point": ".", "percent": "%",
           "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
           "year": "{}", "day": "{mon} {d}, {y}",
           "title": "Argonus backtest on a {start} account",
           "subtitle": "{first} – {last} · trades: {trades} · fees and slippage included",
           "account": "Argonus account", "index": "IMOEX with the same {start}",
           "fresh": "new data", "drawdown": "Drawdown from peak"},
    "zh-CN": {"group": ",", "point": ".", "percent": "%", "months": CJK_MONTHS,
              "year": "{}年", "day": "{y}年{m}月{d}日",
              "title": "Argonus 回测（账户 {start}）",
              "subtitle": "{first} – {last} · 交易笔数：{trades} · 已计入手续费和滑点",
              "account": "Argonus 账户", "index": "IMOEX（同样投入 {start}）",
              "fresh": "新数据", "drawdown": "相对峰值的回撤"},
    "es": {"group": ".", "point": ",", "percent": "\u00a0%",
           "months": "ene feb mar abr may jun jul ago sep oct nov dic".split(),
           "year": "{}", "day": "{d:02d}/{m:02d}/{y}",
           "title": "Backtest de Argonus con una cuenta de {start}",
           "subtitle": "{first} – {last} · operaciones: {trades} · comisiones y deslizamiento incluidos",
           "account": "Cuenta de Argonus", "index": "IMOEX con los mismos {start}",
           "fresh": "datos nuevos", "drawdown": "Caída desde el máximo"},
    "pt-BR": {"group": ".", "point": ",", "percent": "%",
              "months": "jan fev mar abr mai jun jul ago set out nov dez".split(),
              "year": "{}", "day": "{d:02d}/{m:02d}/{y}",
              "title": "Backtest do Argonus com uma conta de {start}",
              "subtitle": "{first} – {last} · operações: {trades} · taxas e slippage incluídos",
              "account": "Conta do Argonus", "index": "IMOEX com os mesmos {start}",
              "fresh": "dados novos", "drawdown": "Queda a partir do pico"},
    "ja": {"group": ",", "point": ".", "percent": "%", "months": CJK_MONTHS,
           "year": "{}年", "day": "{y}年{m}月{d}日",
           "title": "Argonus のバックテスト（口座 {start}）",
           "subtitle": "{first} – {last} · 取引数：{trades} · 手数料とスリッページを反映",
           "account": "Argonus の口座", "index": "IMOEX（同額 {start} を投資）",
           "fresh": "新しいデータ", "drawdown": "最高値からのドローダウン"},
    "ko": {"group": ",", "point": ".", "percent": "%",
           "months": [f"{month}월" for month in range(1, 13)],
           "year": "{}년", "day": "{y}년 {m}월 {d}일",
           "title": "Argonus 백테스트 (계좌 {start})",
           "subtitle": "{first} – {last} · 거래 수: {trades} · 수수료와 슬리피지 반영",
           "account": "Argonus 계좌", "index": "IMOEX (같은 {start} 투자)",
           "fresh": "새 데이터", "drawdown": "고점 대비 낙폭"},
    "de": {"group": ".", "point": ",", "percent": "\u00a0%",
           "months": "Jan Feb Mär Apr Mai Jun Jul Aug Sep Okt Nov Dez".split(),
           "year": "{}", "day": "{d:02d}.{m:02d}.{y}",
           "title": "Argonus-Backtest mit {start} Startkapital",
           "subtitle": "{first} – {last} · Trades: {trades} · Gebühren und Slippage berücksichtigt",
           "account": "Argonus-Konto", "index": "IMOEX mit denselben {start}",
           "fresh": "neue Daten", "drawdown": "Drawdown vom Höchststand"},
}
CJK = {"zh-CN", "ja", "ko"}


def load_series(backtest_path, index_path):
    report = json.loads(Path(backtest_path).read_text())
    candidate = report["candidate"]
    daily = candidate["daily"]
    with gzip.open(index_path, "rt") as handle:
        closes = sorted((date.fromisoformat(row[0]), float(row[4]))
                        for row in json.load(handle)["analyzer_index"])
    first = date.fromisoformat(daily[0]["date"])
    start = max(day for day, _ in closes if day < first)
    dates = [start] + [date.fromisoformat(row["date"]) for row in daily]
    equity = [daily[0]["equity_before"]] + [row["equity_after"] for row in daily]
    if abs(equity[-1] - candidate["ending_equity_rub"]) > 0.01:
        raise ValueError("Daily equity does not reach the reported ending balance")
    if closes[-1][0] < dates[-1]:
        raise ValueError(f"IMOEX closes end {closes[-1][0]}, before the backtest end {dates[-1]}")
    base = dict(closes)[start]
    index, position, last = [], 0, None
    for day in dates:
        while position < len(closes) and closes[position][0] <= day:
            last = closes[position][1]
            position += 1
        index.append(equity[0] * last / base)
    peak, drawdown = equity[0], []
    for value in equity:
        peak = max(peak, value)
        drawdown.append((value / peak - 1) * 100)
    return {"dates": dates, "equity": equity, "index": index, "drawdown": drawdown,
            "trades": candidate["trades"]}


def number(value, language, decimals=0):
    style = LANGUAGES[language]
    text = f"{abs(value):,.{decimals}f}".replace(",", "\0").replace(".", style["point"])
    return ("\u2212" if value < 0 else "") + text.replace("\0", style["group"])


def money(value, language):
    return number(value, language) + "\u00a0₽"


def percent(value, language, decimals=0, sign=True):
    prefix = "+" if sign and value > 0 else ""
    return prefix + number(value, language, decimals) + LANGUAGES[language]["percent"]


def day_label(day, language):
    style = LANGUAGES[language]
    return style["day"].format(d=day.day, m=day.month, y=day.year,
                               mon=style["months"][day.month - 1])


def month_ticks(first, last, language):
    style = LANGUAGES[language]
    ticks, labels = [], []
    month = date(first.year, first.month, 1)
    while month <= last:
        if month >= first:
            label = style["months"][month.month - 1]
            if not ticks or month.month == 1:
                label += "\n" + style["year"].format(month.year)
            ticks.append(month)
            labels.append(label)
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return ticks, labels


def render(series, language, theme, path):
    colors, text = THEMES[theme], LANGUAGES[language]
    plt.rcParams.update({
        # DejaVu Sans supplies the true minus sign that Noto Sans lacks.
        "font.family": ["Noto Sans", "DejaVu Sans"] + (["Noto Sans CJK JP"] if language in CJK else []),
        "svg.hashsalt": "argonus-backtest",
        "axes.unicode_minus": True,
    })
    dates, equity, index = series["dates"], series["equity"], series["index"]
    drawdown = series["drawdown"]
    start = equity[0]

    width, height = 8.8, 5.85
    left, right, rail = 1.0, 1.05, 0.22
    fig = plt.figure(figsize=(width, height), facecolor=colors["surface"])
    main = fig.add_axes([left / width, 1.98 / height, (width - left - right) / width, 2.62 / height])
    lower = fig.add_axes([left / width, 0.55 / height, (width - left - right) / width, 1.05 / height],
                         sharex=main)

    fig.text(rail / width, 1 - 0.2 / height, text["title"].format(start=money(start, language)),
             color=colors["primary"], fontsize=12.5, fontweight="bold", va="top")
    fig.text(rail / width, 1 - 0.5 / height, text["subtitle"].format(
        first=day_label(dates[1], language), last=day_label(dates[-1], language),
        trades=number(series["trades"], language)),
        color=colors["secondary"], fontsize=9.5, va="top")
    handles = [Line2D([], [], color=colors["account"], lw=2.2),
               Line2D([], [], color=colors["index"], lw=2.2)]
    fig.legend(handles, [text["account"], text["index"].format(start=money(start, language))],
               loc="upper left", bbox_to_anchor=(rail / width - 0.006, 1 - 0.74 / height),
               ncol=2, frameon=False, fontsize=9.5, labelcolor=colors["secondary"],
               handlelength=1.5, handletextpad=0.6, columnspacing=1.8, borderaxespad=0)

    x_end = dates[-1] + timedelta(days=3)
    for axis in (main, lower):
        axis.set_facecolor(colors["surface"])
        axis.axvspan(FRESH_FROM, x_end, color=colors["band"], lw=0, zorder=0)
        axis.set_xlim(dates[0], x_end)
        axis.grid(axis="y", color=colors["grid"], lw=0.75)
        axis.set_axisbelow(True)
        axis.tick_params(length=0, colors=colors["secondary"], labelsize=9, pad=6)
        for spine in axis.spines.values():
            spine.set_visible(False)

    main.set_ylim(0, 175_000)
    main.set_yticks(range(0, 150_001, 50_000))
    main.set_yticklabels([money(tick, language) for tick in range(0, 150_001, 50_000)])
    main.axhline(0, color=colors["baseline"], lw=1)
    main.plot(dates, index, color=colors["index"], lw=1.4, zorder=2,
              solid_joinstyle="round", solid_capstyle="round")
    main.plot(dates, equity, color=colors["account"], lw=1.7, zorder=3,
              solid_joinstyle="round", solid_capstyle="round")
    main.tick_params(axis="x", labelbottom=False)
    main.text(FRESH_FROM + timedelta(days=4), 6_000, text["fresh"],
              color=colors["secondary"], fontsize=8.5, va="bottom")
    for values, key in ((equity, "account"), (index, "index")):
        main.plot([dates[-1]], [values[-1]], "o", ms=6.5, mfc=colors[key], mec=colors["surface"],
                  mew=1.6, zorder=4, clip_on=False)
        for offset, label, weight, color in (
                (6, money(values[-1], language), "bold", colors["primary"]),
                (-7, percent((values[-1] / start - 1) * 100, language), "normal", colors["secondary"])):
            main.annotate(label, (dates[-1], values[-1]), xytext=(9, offset), textcoords="offset points",
                          va="center", fontsize=9.5, fontweight=weight, color=color,
                          annotation_clip=False)

    lower.set_ylim(-14.5, 1)
    lower.set_yticks([0, -5, -10])
    lower.set_yticklabels([percent(tick, language, sign=False) for tick in (0, -5, -10)])
    lower.axhline(0, color=colors["baseline"], lw=1)
    lower.fill_between(dates, drawdown, 0, color=colors["account"], alpha=0.1, lw=0, zorder=1)
    lower.plot(dates, drawdown, color=colors["account"], lw=1.2, zorder=2,
               solid_joinstyle="round", solid_capstyle="round")
    trough = min(range(len(drawdown)), key=drawdown.__getitem__)
    lower.plot([dates[trough]], [drawdown[trough]], "o", ms=5.5, mfc=colors["account"], mew=0, zorder=3)
    lower.annotate(percent(drawdown[trough], language, decimals=1), (dates[trough], drawdown[trough]),
                   xytext=(7, 0), textcoords="offset points", va="center", fontsize=9,
                   color=colors["primary"])
    fig.text(rail / width, 1.7 / height, text["drawdown"],
             color=colors["secondary"], fontsize=9.5, va="bottom")
    ticks, labels = month_ticks(dates[0], dates[-1], language)
    lower.set_xticks(ticks)
    lower.set_xticklabels(labels, linespacing=1.3)
    lower.tick_params(axis="x", pad=7)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=colors["surface"], metadata={"Date": None})
    plt.close(fig)


def summary(series):
    dates, equity, index, drawdown = (series[key] for key in ("dates", "equity", "index", "drawdown"))
    fresh = max(i for i, day in enumerate(dates) if day < FRESH_FROM)
    trough = min(range(len(drawdown)), key=drawdown.__getitem__)
    return {
        "period": [dates[1].isoformat(), dates[-1].isoformat()],
        "ending_equity_rub": round(equity[-1], 2),
        "return_pct": round((equity[-1] / equity[0] - 1) * 100, 2),
        "imoex_return_pct": round((index[-1] / index[0] - 1) * 100, 2),
        "max_drawdown_pct": round(drawdown[trough], 2),
        "max_drawdown_date": dates[trough].isoformat(),
        "before_fresh_return_pct": round((equity[fresh] / equity[0] - 1) * 100, 2),
        "fresh_return_pct": round((equity[-1] / equity[fresh] - 1) * 100, 2),
        "fresh_imoex_return_pct": round((index[-1] / index[fresh] - 1) * 100, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backtest", type=Path, default=BACKTEST)
    parser.add_argument("--index-cache", type=Path, default=INDEX_CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    series = load_series(args.backtest, args.index_cache)
    for language in LANGUAGES:
        for theme in THEMES:
            render(series, language, theme, args.output_dir / f"equity-{language}-{theme}.svg")
    print(json.dumps(summary(series), indent=2))


if __name__ == "__main__":
    main()
