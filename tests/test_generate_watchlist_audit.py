#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from argonus.watchlists import generate_watchlist as gw


class GeneratorAuditTests(unittest.TestCase):
    AS_OF = date.today()

    @classmethod
    def make_candidate(
        cls,
        symbol: str,
        direction: str,
        score: float,
        *,
        include_future: bool = False,
    ) -> gw.Candidate:
        sessions: list[gw.SessionData] = []
        start = cls.AS_OF - timedelta(days=40)
        for index in range(40):
            trade_date = start + timedelta(days=index)
            close = 100.0 + index * 0.2
            sessions.append(
                gw.SessionData(
                    trade_date=trade_date,
                    open=close - 0.1,
                    low=close - 1.0,
                    high=close + 1.0,
                    close=close,
                    volume=60_000_000.0 + index,
                )
            )
        if include_future:
            sessions[-1] = gw.SessionData(
                trade_date=cls.AS_OF,
                open=110.0,
                low=109.0,
                high=111.0,
                close=110.5,
                volume=70_000_000.0,
            )
        latest = sessions[-1].close
        sign = 1.0 if direction == "long" else -1.0
        return gw.Candidate(
            symbol=symbol,
            direction=direction,
            score=score,
            reason=f"audit-{symbol}",
            sessions=sessions,
            avg_day_rub=2.0,
            avg_day_pct=2.0,
            support=latest - 1.0,
            resistance=latest + 1.0,
            targets=[latest + sign * move for move in (0.7, 1.1, 1.6, 2.2)],
            instrument_uid=f"uid-{symbol.lower()}",
            lot_size=10,
            api_trade_available=True,
            buy_available=True,
            sell_available=True,
            short_enabled=direction == "short",
            exchange="MOEX",
            real_exchange="REAL_EXCHANGE_MOEX",
        )

    def test_future_session_is_rejected_instead_of_silently_trimmed(self) -> None:
        leaked = self.make_candidate("LEAK", "long", 80.0, include_future=True)
        result = gw.ScanResult(
            selected=[leaked],
            eligible_longs=[leaked],
            eligible_shorts=[],
        )

        with self.assertRaisesRegex(RuntimeError, "future leak"):
            gw.build_audit_payload(
                self.AS_OF,
                result,
                top_n=1,
                min_volume_rub=50_000_000.0,
                generated_at="2026-07-20T06:40:00+03:00",
            )

    def test_payload_keeps_full_sorted_pools_and_marks_legacy_selection(self) -> None:
        longs = [
            self.make_candidate("L1", "long", 90.0),
            self.make_candidate("L2", "long", 70.0),
            self.make_candidate("L3", "long", 50.0),
            self.make_candidate("L4", "long", 30.0),
        ]
        shorts = [
            self.make_candidate("S1", "short", 80.0),
            self.make_candidate("S2", "short", 60.0),
            self.make_candidate("S3", "short", 40.0),
        ]
        selected = longs[:2] + shorts[:2]
        result = gw.ScanResult(
            selected=selected,
            eligible_longs=longs,
            eligible_shorts=shorts,
        )

        payload = gw.build_audit_payload(
            self.AS_OF,
            result,
            top_n=2,
            min_volume_rub=50_000_000.0,
            generated_at="2026-07-20T06:40:00+03:00",
        )

        self.assertEqual(payload["pool_counts"], {"long": 4, "short": 3, "selected": 4})
        self.assertEqual(
            [(row["symbol"], row["direction"]) for row in payload["selected"]],
            [(item.symbol, item.direction) for item in selected],
        )
        long_pool = payload["eligible_pools"]["long"]
        short_pool = payload["eligible_pools"]["short"]
        self.assertEqual([row["rank"] for row in long_pool], [1, 2, 3, 4])
        self.assertEqual([row["raw_score"] for row in long_pool], [90.0, 70.0, 50.0, 30.0])
        self.assertEqual([row["selected"] for row in long_pool], [True, True, False, False])
        self.assertEqual([row["selected"] for row in short_pool], [True, True, False])
        self.assertNotIn("sessions", long_pool[0])
        self.assertEqual(
            long_pool[0]["instrument_snapshot"],
            {
                "instrument_uid": "uid-l1",
                "lot_size": 10,
                "api_trade_available": True,
                "buy_available": True,
                "sell_available": True,
                "short_enabled": False,
                "exchange": "MOEX",
                "real_exchange": "REAL_EXCHANGE_MOEX",
                "captured_by": "InstrumentsService/Shares before candidate selection",
            },
        )
        self.assertLess(
            date.fromisoformat(long_pool[0]["features"]["last_session_date"]),
            self.AS_OF,
        )
        self.assertTrue(payload["history_cutoff"]["causal_cutoff_verified"])
        self.assertEqual(payload["capture_mode"], gw.CAPTURE_MODE_FORWARD)
        self.assertTrue(payload["provenance"]["forward_archive_eligible"])

    def test_day_archive_is_immutable_and_current_sidecar_can_advance(self) -> None:
        candidate = self.make_candidate("L1", "long", 90.0)
        result = gw.ScanResult(
            selected=[candidate],
            eligible_longs=[candidate],
            eligible_shorts=[],
        )
        first = gw.build_audit_payload(
            self.AS_OF,
            result,
            top_n=1,
            min_volume_rub=50_000_000.0,
            generated_at="2026-07-20T06:40:00+03:00",
        )
        second = dict(first)
        second["generated_at"] = "2026-07-20T07:00:00+03:00"

        with tempfile.TemporaryDirectory() as directory:
            current = os.path.join(directory, gw.DEFAULT_AUDIT_FILENAME)
            archive, created_first = gw.write_audit_artifacts(first, current)
            archive_again, created_second = gw.write_audit_artifacts(second, current)

            self.assertEqual(archive_again, archive)
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            with open(archive, "r", encoding="utf-8") as handle:
                archived_payload = json.load(handle)
            with open(current, "r", encoding="utf-8") as handle:
                current_payload = json.load(handle)
            self.assertEqual(archived_payload["generated_at"], first["generated_at"])
            self.assertEqual(current_payload["generated_at"], second["generated_at"])

    def test_non_live_as_of_dates_are_routed_to_research_archives(self) -> None:
        candidate = self.make_candidate("L1", "long", 90.0)
        result = gw.ScanResult(
            selected=[candidate],
            eligible_longs=[candidate],
            eligible_shorts=[],
        )
        historical = gw.build_audit_payload(
            self.AS_OF,
            result,
            top_n=1,
            min_volume_rub=50_000_000.0,
            generated_at="2026-07-21T06:40:00+03:00",
            capture_date=self.AS_OF + timedelta(days=1),
        )
        future = gw.build_audit_payload(
            self.AS_OF,
            result,
            top_n=1,
            min_volume_rub=50_000_000.0,
            generated_at="2026-07-19T06:40:00+03:00",
            capture_date=self.AS_OF - timedelta(days=1),
        )

        self.assertEqual(historical["capture_mode"], gw.CAPTURE_MODE_HISTORICAL)
        self.assertEqual(future["capture_mode"], gw.CAPTURE_MODE_FUTURE)
        self.assertFalse(historical["provenance"]["forward_archive_eligible"])
        self.assertFalse(future["provenance"]["forward_archive_eligible"])
        self.assertIn("current API snapshot", historical["provenance"]["note"])
        self.assertIn("current API snapshot", future["provenance"]["note"])
        self.assertIn("not a verified forward observation", future["provenance"]["note"])

        with tempfile.TemporaryDirectory() as directory:
            current = os.path.join(directory, gw.DEFAULT_AUDIT_FILENAME)
            historical_path, _ = gw.write_audit_artifacts(historical, current)
            future_path, _ = gw.write_audit_artifacts(future, current)
            self.assertIn(
                os.path.join(gw.RESEARCH_AUDIT_DIRNAME, gw.CAPTURE_MODE_HISTORICAL),
                historical_path,
            )
            self.assertIn(
                os.path.join(gw.RESEARCH_AUDIT_DIRNAME, gw.CAPTURE_MODE_FUTURE),
                future_path,
            )
            self.assertFalse(os.path.exists(os.path.join(directory, gw.AUDIT_ARCHIVE_DIRNAME)))

    def test_json_cli_keeps_legacy_selected_output_and_writes_full_sidecar(self) -> None:
        longs = [
            self.make_candidate("L1", "long", 90.0),
            self.make_candidate("L2", "long", 70.0),
            self.make_candidate("L3", "long", 50.0),
        ]
        shorts = [
            self.make_candidate("S1", "short", 80.0),
            self.make_candidate("S2", "short", 60.0),
            self.make_candidate("S3", "short", 40.0),
        ]
        result = gw.ScanResult(
            selected=longs[:2] + shorts[:2],
            eligible_longs=longs,
            eligible_shorts=shorts,
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "selected.json")
            argv = [
                "argonus/watchlists/generate_watchlist.py",
                "--as-of",
                self.AS_OF.isoformat(),
                "--top",
                "2",
                "--json",
                "-o",
                output_path,
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                gw,
                "_scan_and_select_result",
                return_value=result,
            ):
                self.assertEqual(gw.main(), 0)

            with open(output_path, "r", encoding="utf-8") as handle:
                legacy_json = json.load(handle)
            self.assertEqual(
                [(row["symbol"], row["direction"]) for row in legacy_json],
                [(item.symbol, item.direction) for item in result.selected],
            )
            self.assertNotIn("eligible_pools", legacy_json[0])

            current_path = os.path.join(directory, gw.DEFAULT_AUDIT_FILENAME)
            archive_path = os.path.join(
                directory,
                gw.AUDIT_ARCHIVE_DIRNAME,
                f"{self.AS_OF.isoformat()}.json",
            )
            self.assertTrue(os.path.isfile(current_path))
            self.assertTrue(os.path.isfile(archive_path))
            with open(current_path, "r", encoding="utf-8") as handle:
                audit = json.load(handle)
            self.assertEqual(audit["pool_counts"], {"long": 3, "short": 3, "selected": 4})

    def test_audit_io_failure_does_not_block_valid_watchlist(self) -> None:
        long_candidate = self.make_candidate("L1", "long", 90.0)
        short_candidate = self.make_candidate("S1", "short", 80.0)
        result = gw.ScanResult(
            selected=[long_candidate, short_candidate],
            eligible_longs=[long_candidate],
            eligible_shorts=[short_candidate],
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "data/watchlists/watchlist.txt")
            argv = [
                "argonus/watchlists/generate_watchlist.py",
                "--as-of",
                self.AS_OF.isoformat(),
                "-o",
                output_path,
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                gw,
                "_scan_and_select_result",
                return_value=result,
            ), mock.patch.object(
                gw,
                "write_audit_artifacts",
                side_effect=OSError("read-only telemetry mount"),
            ):
                self.assertEqual(gw.main(), 0)

            with open(output_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), gw.format_watchlist(result.selected))

    def test_output_failure_publishes_no_current_or_forward_audit(self) -> None:
        candidate = self.make_candidate("L1", "long", 90.0)
        result = gw.ScanResult(
            selected=[candidate],
            eligible_longs=[candidate],
            eligible_shorts=[],
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "data/watchlists/watchlist.txt")
            current_path = os.path.join(directory, gw.DEFAULT_AUDIT_FILENAME)
            forward_path = os.path.join(
                directory,
                gw.AUDIT_ARCHIVE_DIRNAME,
                f"{self.AS_OF.isoformat()}.json",
            )
            argv = [
                "argonus/watchlists/generate_watchlist.py",
                "--as-of",
                self.AS_OF.isoformat(),
                "-o",
                output_path,
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                gw,
                "_scan_and_select_result",
                return_value=result,
            ), mock.patch.object(
                gw,
                "_atomic_write_text",
                side_effect=OSError("watchlist disk unavailable"),
            ), mock.patch.object(gw, "write_audit_artifacts") as audit_writer:
                with self.assertRaisesRegex(OSError, "watchlist disk unavailable"):
                    gw.main()

            audit_writer.assert_not_called()
            self.assertFalse(os.path.exists(output_path))
            self.assertFalse(os.path.exists(current_path))
            self.assertFalse(os.path.exists(forward_path))


if __name__ == "__main__":
    unittest.main()
