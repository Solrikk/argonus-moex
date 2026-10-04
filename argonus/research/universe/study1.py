#!/usr/bin/env python3
"""Этап 2, часть 2: грид стратегий на features.json.
ДИСЦИПЛИНА: по умолчанию работает ТОЛЬКО на окне подбора Nov'25–Mar'26.
Экзаменационное окно Apr–Jul'26 запускается один раз флагом --exam CONFIG_ID.
Шаблон: топ-1 кандидат в день по скору, вход 07:30, стоп 1%, цель 2/3%, выход 18:35.
Нетто-комиссия 0.08%/круг."""
from __future__ import annotations

from argonus.paths import UNIVERSE_DIR, WATCHLIST_DIR, PROJECT_ROOT
import argparse, json, os
from collections import defaultdict

HERE = str(UNIVERSE_DIR)
FEE = 0.08
FIT = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
EXAM = ["2026-04", "2026-05", "2026-06", "2026-07"]

def load():
    return json.load(open(os.path.join(HERE, "features.json")))

def direction_of(family, r):
    if family.startswith("mom"):
        v = r["mom30"]
    elif family.startswith("rs5"):
        v = r["rs5"]
    elif family.startswith("rs1"):
        v = r["rs1"]
    elif family.startswith("gap"):
        v = r["gap"]
    elif family.startswith("rangepos"):
        v = (r["range_pos"] - 0.5) if r["range_pos"] is not None else None
    else:
        return None, None
    if v is None: return None, None
    base = "long" if v > 0 else "short"
    if family.endswith("_fade"): base = "short" if base == "long" else "long"
    return base, abs(v)

CONFIGS = []
for fam in ["mom_follow", "mom_fade", "rs5_follow", "rs5_fade", "rs1_follow",
            "gap_follow", "gap_fade", "rangepos_follow", "rangepos_fade"]:
    for gate in [0, 1]:
        for tgt in [20, 30]:
            CONFIGS.append({"id": f"{fam}|gate{gate}|t{tgt}", "family": fam,
                            "gate": gate, "target": tgt})

def run(rows, cfg, months):
    per_day = defaultdict(list)
    for r in rows:
        if r["month"] not in months: continue
        d, score = direction_of(cfg["family"], r)
        if d is None or score is None: continue
        if cfg["gate"]:
            if d == "long" and not r["bullish"]: continue
            if d == "short" and r["bullish"]: continue
        ret = r[f"ret_{d}_t{cfg['target']}"]
        if ret is None: continue
        per_day[r["date"]].append((score, ret))
    by_m = defaultdict(list)
    for ds, cands in per_day.items():
        cands.sort(key=lambda x: -x[0])
        by_m[ds[:7]].append(cands[0][1])
    cap = 1.0; peak = 1.0; mdd = 0.0; neg = 0; n = 0; mrets = {}
    for m in months:
        f = 1.0
        for x in by_m.get(m, []):
            f *= (1 + (x - FEE) / 100)
        cap *= f; peak = max(peak, cap); mdd = min(mdd, cap / peak - 1)
        if f < 1: neg += 1
        n += len(by_m.get(m, []))
        mrets[m] = (f - 1) * 100
    return {"total": (cap - 1) * 100, "neg": neg, "mdd": mdd * 100, "n": n, "months": mrets}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exam", default=None, help="CONFIG_ID для ЕДИНСТВЕННОГО прогона на Apr–Jul")
    args = ap.parse_args()
    rows = load()
    if args.exam:
        cfg = next(c for c in CONFIGS if c["id"] == args.exam)
        res = run(rows, cfg, EXAM)
        print(f"ЭКЗАМЕН Apr–Jul: {cfg['id']}")
        print(f"  total={res['total']:+.2f}% negMo={res['neg']} maxDD={res['mdd']:.2f}% n={res['n']}")
        print("  " + " ".join(f"{m}={v:+.1f}" for m, v in res["months"].items()))
        return 0
    print(f"ОКНО ПОДБОРА {FIT[0]}..{FIT[-1]} | конфигов: {len(CONFIGS)}")
    scored = []
    for cfg in CONFIGS:
        res = run(rows, cfg, FIT)
        scored.append((res["total"], cfg, res))
    scored.sort(key=lambda x: -x[0])
    print(f"{'config':28} {'n':>4} {'total%':>8} {'negMo':>5} {'maxDD%':>7}  помесячно")
    for tot, cfg, res in scored[:14]:
        mm = " ".join(f"{v:+.1f}" for _, v in sorted(res["months"].items()))
        print(f"{cfg['id']:28} {res['n']:>4} {tot:>8.2f} {res['neg']:>5} {res['mdd']:>7.2f}  {mm}")
    print("... (хвост скрыт)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
