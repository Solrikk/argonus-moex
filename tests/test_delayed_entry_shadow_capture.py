#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from argonus.shadow import capture_delayed_entry_0705 as capture_cli
from argonus.shadow import delayed_entry_shadow as subject


MOSCOW = ZoneInfo("Europe/Moscow")
MANIFEST = {"manifest_sha256": "a" * 64, "evidence_not_before": "2026-07-20"}


def plan(*, target: float = 102.0, created: str = "2026-07-20T07:05:01+03:00") -> dict:
    record = {
        "schema_version": 1,
        "record_type": subject.PLAN_RECORD_TYPE,
        "capture_mode": "forward",
        "policy": subject.POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "created_at": datetime.fromisoformat(created).isoformat(timespec="milliseconds"),
        "trade_date": "2026-07-20",
        "symbol": "TEST",
        "uid": "uid-test",
        "direction": "long",
        "target_price": target,
        "assumed_round_trip_cost_pct": 0.08,
        "control": {
            "actual_fill_price": 100.0,
            "actual_fill_timestamp": "2026-07-20T07:01:02.000+03:00",
            "actual_filled_lots": 8,
            "lot_size": 10,
        },
        "challenger": {
            "capture_start": subject.CAPTURE_START,
            "capture_end": subject.CAPTURE_END,
            "book_depth": subject.BOOK_DEPTH,
            "max_quote_age_ms": subject.MAX_QUOTE_AGE_MS,
            "requested_lots": 8,
            "side": "asks",
        },
        "source_exit_plan_id": "b" * 64,
        "source_exit_plan_sha256": "c" * 64,
        "source_exit_manifest_sha256": "d" * 64,
        "source_state_sha256": "e" * 64,
        "manifest_sha256": MANIFEST["manifest_sha256"],
    }
    record["plan_id"] = subject.canonical_sha256(record)
    return record


def book(*, timestamp: str = "2026-07-20T07:05:05+03:00") -> dict:
    return {
        "depth": 50,
        "instrumentUid": "uid-test",
        "ticker": "TEST",
        "orderbookTs": timestamp,
        "bids": [
            {"price": 99.9, "quantity": 20},
            {"price": 99.8, "quantity": 20},
        ],
        "asks": [
            {"price": 100.1, "quantity": 5},
            {"price": 100.2, "quantity": 5},
        ],
    }


class CoreCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest_patch = patch.object(subject, "_manifest_or_default", return_value=MANIFEST)
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)

    def test_depth_vwap_is_side_specific_and_uses_lot_quantities(self) -> None:
        result = subject.build_capture(
            plan(),
            book(),
            request_started_at="2026-07-20T07:05:04+03:00",
            response_received_at="2026-07-20T07:05:07+03:00",
            manifest=MANIFEST,
        )
        self.assertAlmostEqual(result["challenger"]["executable_vwap"], 100.1375)
        self.assertEqual(result["challenger"]["covered_lots"], 8)
        self.assertEqual(result["challenger"]["worst_price"], 100.2)
        self.assertEqual(result["challenger"]["evaluation_start_time"], "07:10")
        self.assertTrue(result["promotion_eligible"])
        self.assertFalse(result["execution_claim"])
        self.assertEqual(subject.validate_capture(result, manifest=MANIFEST), result)

    def test_target_must_be_ahead_of_worst_consumed_level_not_vwap(self) -> None:
        result = subject.build_capture(
            plan(target=100.15),
            book(),
            request_started_at="2026-07-20T07:05:04+03:00",
            response_received_at="2026-07-20T07:05:07+03:00",
            manifest=MANIFEST,
        )
        self.assertLess(result["challenger"]["executable_vwap"], 100.15)
        self.assertGreater(result["challenger"]["worst_price"], 100.15)
        self.assertFalse(result["gates"]["target_ahead"])
        self.assertFalse(result["promotion_eligible"])

    def test_stale_or_incomplete_depth_is_diagnostic_only(self) -> None:
        shallow = book()
        shallow["asks"] = [{"price": 100.1, "quantity": 2}]
        result = subject.build_capture(
            plan(),
            shallow,
            request_started_at="2026-07-20T07:05:08+03:00",
            response_received_at="2026-07-20T07:05:10+03:00",
            manifest=MANIFEST,
        )
        self.assertIsNone(result["challenger"]["executable_vwap"])
        self.assertFalse(result["gates"]["depth_coverage"])
        self.assertFalse(result["gates"]["quote_freshness"])
        self.assertEqual(subject.validate_capture(result, manifest=MANIFEST), result)

    def test_capture_identity_tamper_is_rejected(self) -> None:
        result = subject.build_capture(
            plan(), book(),
            request_started_at="2026-07-20T07:05:04+03:00",
            response_received_at="2026-07-20T07:05:07+03:00",
            manifest=MANIFEST,
        )
        tampered = deepcopy(result)
        tampered["challenger"]["executable_vwap"] = 1.0
        with self.assertRaisesRegex(ValueError, "capture_id"):
            subject.validate_capture(tampered, manifest=MANIFEST)

    def test_missing_uid_timestamp_or_crossed_book_is_rejected(self) -> None:
        for mutation, pattern in (
            (lambda value: value.pop("instrumentUid"), "uid mismatch"),
            (lambda value: value.pop("orderbookTs"), "mandatory orderbookTs"),
            (lambda value: value["bids"].__setitem__(0, {"price": 100.1, "quantity": 20}), "crossed"),
        ):
            with self.subTest(pattern=pattern):
                value = book()
                mutation(value)
                with self.assertRaisesRegex(ValueError, pattern):
                    subject.build_capture(
                        plan(), value,
                        request_started_at="2026-07-20T07:05:04+03:00",
                        response_received_at="2026-07-20T07:05:07+03:00",
                        manifest=MANIFEST,
                    )

    def test_atomic_first_writer_returns_created_only_once(self) -> None:
        frozen = plan()
        with tempfile.TemporaryDirectory() as directory:
            first_path, first = subject.write_first_writer_wins(
                frozen, directory, current_date=date(2026, 7, 20), manifest=MANIFEST
            )
            second_path, second = subject.write_first_writer_wins(
                frozen, directory, current_date=date(2026, 7, 20), manifest=MANIFEST
            )
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(first_path, second_path)
            self.assertEqual(json.loads(first_path.read_text(encoding="utf-8")), frozen)
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])


class StrictManifestTests(unittest.TestCase):
    def test_registered_artifacts_and_statistical_gates_are_pinned(self) -> None:
        manifest = subject.load_manifest()
        self.assertEqual(manifest["policy"]["capture_start"], "07:05:00")
        self.assertEqual(manifest["policy"]["capture_end"], "07:05:15")
        self.assertEqual(manifest["policy"]["max_quote_age_ms"], 3000)
        self.assertEqual(manifest["promotion_gates"]["minimum_eligible_captures"], 50)
        self.assertEqual(
            manifest["promotion_gates"]["positive_delta_required_at_stress_bps"], 10
        )
        self.assertEqual(len(manifest["manifest_sha256"]), 64)


class ProtectedPlanTests(unittest.TestCase):
    def test_plan_requires_authoritative_fill_stop_and_entered_state(self) -> None:
        frozen_exit = {
            "trade_date": "2026-07-20",
            "symbol": "TEST",
            "direction": "long",
            "entry_price": 100.0,
            "target_price": 102.0,
            "actual_entry_timestamp": "2026-07-20T07:01:02+03:00",
            "assumed_round_trip_cost_pct": 0.08,
            "plan_id": "b" * 64,
            "manifest_sha256": "d" * 64,
            "optional_evidence": {
                "actual_fill_authoritative": True,
                "stop_order_id": "stop-id",
                "protection_status": "protected",
                "target_guard_failed": False,
                "filled_lots": 8,
                "lot_size": 10,
                "selector_shadow_decision_id": "f" * 64,
                "selector_decision_id": "selector-id",
            },
        }
        state = {
            "date": "2026-07-20", "engine": "A", "phase": "entered",
            "protection_status": "protected", "target_guard_failed": False,
            "symbol": "TEST", "uid": "uid-test", "lots": 8, "lot_size": 10,
            "entry_price": 100.0, "planned_target_price": 102.0,
            "stop_order_id": "stop-id", "selector_decision_id": "selector-id",
        }
        with (
            patch.object(subject, "_manifest_or_default", return_value=MANIFEST),
            patch.object(subject.exit_registry, "validate_exit_shadow_plan", return_value=frozen_exit),
            patch.object(subject.exit_registry, "canonical_sha256", return_value="c" * 64),
        ):
            result = subject.build_plan(
                frozen_exit, state,
                created_at="2026-07-20T07:05:01+03:00", manifest=MANIFEST,
            )
            self.assertEqual(result["control"]["actual_filled_lots"], 8)
            bad_state = {**state, "phase": "closed"}
            with self.assertRaisesRegex(ValueError, "entered phase"):
                subject.build_plan(
                    frozen_exit, bad_state,
                    created_at="2026-07-20T07:05:01+03:00", manifest=MANIFEST,
                )


class CaptureCommandSafetyTests(unittest.TestCase):
    def test_get_order_book_is_single_allowlisted_call(self) -> None:
        client = Mock(retry_count=1)
        client._post.return_value = {"depth": 50}
        capture_cli.get_order_book_once(client, "uid-test")
        client._post.assert_called_once_with(
            capture_cli.GET_ORDER_BOOK_METHOD,
            {"instrumentId": "uid-test", "depth": 50},
        )
        self.assertEqual(
            capture_cli.GET_ORDER_BOOK_METHOD,
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook",
        )

    def test_existing_plan_never_constructs_client_or_requotes(self) -> None:
        factory = Mock(side_effect=AssertionError("client must not be constructed"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_path = root / "exit.json"
            state_path = root / "state.json"
            manifest_path = root / "manifest.json"
            exit_path.write_text("{}", encoding="utf-8")
            manifest_path.write_text("{}", encoding="utf-8")
            state_path.write_text(
                json.dumps({"date": "2026-07-20", "engine": "A"}),
                encoding="utf-8",
            )
            with (
                patch.object(capture_cli.registry, "load_manifest", return_value=MANIFEST),
                patch.object(capture_cli.registry, "build_plan", return_value={"uid": "uid-test"}),
                patch.object(
                    capture_cli.registry,
                    "write_first_writer_wins",
                    return_value=(root / "existing-plan.json", False),
                ),
            ):
                path_value, eligible = capture_cli.capture_once(
                    now=datetime(2026, 7, 20, 7, 5, 1, tzinfo=MOSCOW),
                    exit_plan_path=exit_path,
                    state_path=state_path,
                    output_dir=root / "out",
                    manifest_path=manifest_path,
                    client_factory=factory,
                )
            self.assertFalse(eligible)
            self.assertEqual(path_value, root / "existing-plan.json")
            factory.assert_not_called()

    def test_late_request_is_rejected_before_remote_call(self) -> None:
        client = Mock(retry_count=1)
        factory = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_path = root / "exit.json"
            state_path = root / "state.json"
            manifest_path = root / "manifest.json"
            exit_path.write_text("{}", encoding="utf-8")
            manifest_path.write_text("{}", encoding="utf-8")
            state_path.write_text(
                json.dumps({"date": "2026-07-20", "engine": "A"}), encoding="utf-8"
            )
            writes = iter(((root / "new-plan.json", True),))
            with (
                patch.object(capture_cli.registry, "load_manifest", return_value=MANIFEST),
                patch.object(capture_cli.registry, "build_plan", return_value={"uid": "uid-test"}),
                patch.object(
                    capture_cli.registry,
                    "write_first_writer_wins",
                    side_effect=lambda *_args, **_kwargs: next(writes),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "outside"):
                    capture_cli.capture_once(
                        now=datetime(2026, 7, 20, 7, 5, 1, tzinfo=MOSCOW),
                        exit_plan_path=exit_path,
                        state_path=state_path,
                        output_dir=root / "out",
                        manifest_path=manifest_path,
                        client_factory=factory,
                        clock=lambda: datetime(2026, 7, 20, 7, 5, 16, tzinfo=MOSCOW),
                    )
            client._post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
