"""Offline release validation; no broker construction, account calls or orders."""
import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
from datetime import date
from pathlib import Path

from argonus.paths import PROJECT_ROOT as ROOT
OUTPUT = ROOT/"data/backtests/opening_integration_2026-10-03"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-log", type=Path, default=ROOT / "runtime/test-results.log")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/organization-validation.json")
    args = parser.parse_args()
    # Parse literal exports without executing the live launcher.
    exports = {}
    for line in (ROOT/"scripts/run_tick.sh").read_text().splitlines():
        if line.startswith("export "):
            assignment = shlex.split(line, comments=True)[1]
            key, value = assignment.split("=",1)
            exports[key] = value.replace("$DIR",str(ROOT))
    os.environ.update(exports)
    from argonus.trading import trade_bot
    from argonus.trading import production_opening as opening
    from argonus.research import opening_candidate_paper as paper
    from argonus.market_data import opening_market_data as market

    opening_manifest = opening.validate_manifest("2026-10-03")
    target = trade_bot.validate_profit_target_activation_manifest(trade_date=date(2026,10,3))
    entry = trade_bot.validate_entry_activation_manifest(trade_date=date(2026,10,3))
    assert target[0],target[1]
    assert entry[0],entry[1]
    manifest = json.loads((ROOT/"config/rs5_activation_manifest.json").read_text())
    artifacts = manifest["artifacts"]
    provenance = {key:{"name":artifacts[key]["name"],"sha256":sha(ROOT/"argonus/watchlists"/artifacts[key]["name"])} for key in ("analyzer","selector")}
    provenance["models"] = {key:{"name":v["name"],"sha256":sha(ROOT/"models"/v["name"])} for key,v in artifacts["models"].items()}
    report = {"trade_date":"2026-10-03","feature":manifest["feature"],"threshold_pp":manifest["threshold_pp"],
              "top_k":manifest["top_k"],"provenance":provenance,"runtime_config":trade_bot.profit_target_runtime_config()}
    rs5 = trade_bot._validate_rs5_activation_manifest(str(ROOT/"config/rs5_activation_manifest.json"),report)
    assert rs5[0],rs5[2]
    paper.validate_profile()
    with gzip.open(ROOT/opening_manifest["training_base"],"rt") as handle:
        base = json.load(handle)
    assert base["source_sha256"]==sha(ROOT/"data/backtests/continuous_opening_2026-10-03/expectancy/dataset_full.pkl")
    _,model_meta = market.monthly_models(ROOT/opening_manifest["training_base"],"2026-10")
    data = json.loads((OUTPUT/"all_months.json").read_text())
    months = data["monthly"]
    assert len(months)==13
    previous = 50000.
    for month in months:
        assert abs(month["start_equity_rub"]-previous)<1e-8
        assert abs(month["pnl_rub"]-(month["end_equity_rub"]-previous))<1e-8
        previous = month["end_equity_rub"]
    assert abs(previous-data["candidate"]["ending_equity_rub"])<1e-8
    assert sum(m["trades"] for m in months)==data["candidate"]["trades"]
    assert abs(sum(m["pnl_rub"] for m in months)-data["candidate"]["pnl_rub"])<1e-8
    assert abs(data["control"]["ending_equity_rub"]-136495.47524700494)<1e-6
    backup=OUTPUT/"activation_backup"
    for name,v in json.loads((backup/"manifest.json").read_text()).items():
        assert sha(backup/name)==v["sha256"]
    log=args.test_log.read_text()
    test_count=int(re.search(r"Ran (\d+) tests",log).group(1))
    assert log.rstrip().endswith("OK")
    evidence={"status":"PASS","broker_instantiated":False,"test_orders_submitted":False,"tests":test_count,
              "opening_policy":opening_manifest["policy"],"legacy_target_validation":target[1],
              "legacy_entry_validation":entry[1],"rs5_validation":rs5[1],"frozen_paper_profile":"PASS",
              "monthly_carry_and_ledger_totals":"PASS","legacy_control_reproduction":"PASS",
              "production_model":model_meta,"training_json_rows":len(base["rows"]),
              "manifests":{name:sha(ROOT/name) for name in ("config/opening_activation_manifest.json","config/profit_target_activation_manifest.json","config/entry_0705_activation_manifest.json","config/rs5_activation_manifest.json")},
              "test_log_sha256":sha(args.test_log)}
    market.write_json(args.output,evidence)
    print(json.dumps(evidence,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
