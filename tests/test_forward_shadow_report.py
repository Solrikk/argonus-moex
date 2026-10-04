#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from argonus.shadow import forward_shadow_report as subject
from argonus.shadow import forward_shadow_research as registry
from tests.test_forward_shadow_research import outcome_record


class ForwardShadowReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = registry.load_manifest()

    def test_jsonl_loader_and_report_accept_only_valid_outcomes(self) -> None:
        valid = outcome_record(
            0,
            policy=registry.EXIT_POLICY,
            manifest=self.manifest,
            control=0.0,
            challenger=0.1,
        )
        tampered = json.loads(json.dumps(valid))
        tampered["challenger"]["net_return_pct"] = 10.0
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "outcomes.jsonl"
            ledger.write_text(
                json.dumps(valid) + "\n" + json.dumps(tampered) + "\n",
                encoding="utf-8",
            )
            records = subject.load_outcome_file(ledger)
            report = registry.promotion_metrics(records, policy=registry.EXIT_POLICY)
        self.assertEqual(report["accepted_records"], 1)
        self.assertEqual(report["rejected_records"][0]["reason"], "invalid_outcome")

    def test_cli_is_read_only_and_require_ready_has_status_three(self) -> None:
        valid = outcome_record(
            0,
            policy=registry.EXIT_POLICY,
            manifest=self.manifest,
            control=0.0,
            challenger=0.1,
        )
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "one.json"
            report_path = Path(directory) / "report.json"
            artifact.write_text(json.dumps(valid), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = subject.main(
                    [
                        "--policy",
                        "exit",
                        "--outcomes",
                        str(artifact),
                        "--output",
                        str(report_path),
                        "--require-ready",
                    ]
                )
            printed = json.loads(stdout.getvalue())
            stored = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 3)
        self.assertFalse(printed["promotion_ready"])
        self.assertFalse(stored["production_activation_allowed"])


if __name__ == "__main__":
    unittest.main()
