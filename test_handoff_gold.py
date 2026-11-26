"""Offline checks of the VIIRS handoff Gold validator; no network or credentials.

Small typed Gold tables represent two swaths, a repeated dong in another swath,
and a dong/swath with pixels but no valid temperature. Expectations are fixed
independently of each table mutation so corrupt rows cannot redefine the truth.
"""
import copy
import csv
import hashlib
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

_SPEC = importlib.util.spec_from_file_location(
    "verify_handoff_gold", Path(__file__).resolve().parent / "scripts/verify_handoff_gold.py")
gold = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gold)


class HandoffGoldTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "pipeline_version": "all-scenes-scan-time-v2.1",
            "boundary_version": "sgis-2024-2q", "date_kst": "2024-01-02",
            "input_fingerprint": "a" * 64, "gold_rows_recorded_server": "3",
            "processing_code_sha256": "b" * 64, "boundary_sha256": "c" * 64,
            "source_silver_sha256": "d" * 64,
            "silver_key": "viirs-daily/silver/sgis-2024-2q/2024/01/02/all-observations.parquet",
            "gold_key": "viirs-daily/gold/sgis-2024-2q/2024/01/02/dong-observations.parquet",
            "pixel_count": "12", "finite_temperature": "9", "flagged": "4",
            "valid_flag_zero_temperature": "7",
        }
        self.metadata = {key: self.expected[key] for key in (
            "pipeline_version", "boundary_version", "date_kst", "input_fingerprint",
            "processing_code_sha256", "boundary_sha256", "source_silver_sha256")}
        self.metadata["source_silver"] = self.expected["silver_key"]
        self.metadata["units"] = "Kelvin; TAI93 seconds"
        common = {
            "date": date(2024, 1, 2), "boundary_version": "sgis-2024-2q",
            "swath_start_utc": datetime(2024, 1, 1, 16, tzinfo=timezone.utc),
            "first_scan_start_time_tai93_s": 978364800.0,
            "last_scan_start_time_tai93_s": 978364805.0,
        }
        self.rows = [
            {**common, "dong_code": "11010530", "swath_id": "A2024001.1600",
             "pixel_count": 4, "finite_temperature_count": 3, "flagged_count": 1,
             "valid_count": 2, "temperature_mean_k": 281.0,
             "temperature_min_k": 280.0, "temperature_max_k": 282.0, "valid_fraction": 0.5},
            {**common, "dong_code": "11010540", "swath_id": "A2024001.1600",
             "pixel_count": 3, "finite_temperature_count": 1, "flagged_count": 3,
             "valid_count": 0, "temperature_mean_k": None,
             "temperature_min_k": None, "temperature_max_k": None, "valid_fraction": 0.0},
            {**common, "dong_code": "11010530", "swath_id": "A2024001.1800",
             "swath_start_utc": datetime(2024, 1, 1, 18, tzinfo=timezone.utc),
             "first_scan_start_time_tai93_s": 978372000.0,
             "last_scan_start_time_tai93_s": 978372005.0,
             "pixel_count": 5, "finite_temperature_count": 5, "flagged_count": 0,
             "valid_count": 5, "temperature_mean_k": 290.0,
             "temperature_min_k": 288.0, "temperature_max_k": 292.0, "valid_fraction": 1.0},
        ]
        self.scenes = [
            {"date_kst": "2024-01-02", "swath_id": "A2024001.1600", "time_start_utc": "2024-01-01T16:00:00Z",
             "pixel_count": "7", "finite_temperature": "4",
             "flagged": "4", "valid_flag_zero_temperature": "2"},
            {"date_kst": "2024-01-02", "swath_id": "A2024001.1800", "time_start_utc": "2024-01-01T18:00:00Z",
             "pixel_count": "5", "finite_temperature": "5",
             "flagged": "0", "valid_flag_zero_temperature": "5"},
        ]

    def table(self, rows=None, metadata=None):
        return pa.Table.from_pylist(self.rows if rows is None else rows,
                                   schema=gold.SCHEMA.with_metadata(self.metadata if metadata is None else metadata))

    def check(self, table=None, expected=None, scenes=None):
        # Invalid denominators are deliberate fixtures; validation must report
        # them even if NumPy also emits divide/invalid warnings.
        with np.errstate(divide="ignore", invalid="ignore"):
            return gold.check_table(self.table() if table is None else table,
                                    self.expected if expected is None else expected,
                                    self.scenes if scenes is None else scenes)

    def test_valid_multiswath_table_preserves_zero_valid_group(self):
        result = self.check()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["groups_without_valid_temperature"], 1)
        self.assertEqual(result["distinct_swaths_with_land_pixels"], 2)
        self.assertEqual(result["pixel_count"], 12)
        self.assertEqual(result["valid_count"], 7)
        self.assertEqual(result["temperature_min_k"], 280.0)
        self.assertEqual(result["temperature_max_k"], 292.0)

    def test_entire_day_without_valid_temperature_keeps_null_statistics(self):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            row.update(valid_count=0, valid_fraction=0.0,
                       flagged_count=row["pixel_count"], temperature_mean_k=None,
                       temperature_min_k=None, temperature_max_k=None)
        expected = {**self.expected, "flagged": "12", "valid_flag_zero_temperature": "0"}
        scenes = copy.deepcopy(self.scenes)
        for scene in scenes:
            scene.update(flagged=scene["pixel_count"], valid_flag_zero_temperature="0")
        result = self.check(self.table(rows), expected, scenes)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["groups_without_valid_temperature"], 3)
        self.assertEqual(result["temperature_min_k"], "")
        self.assertEqual(result["temperature_max_k"], "")

    def test_duplicate_dong_swath_key_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[1]["dong_code"] = rows[0]["dong_code"]
        self.assertIn("duplicate_dong_swath_key", self.check(self.table(rows))["errors"])

    def test_wrong_fraction_denominator_range_and_nonfinite_are_rejected(self):
        for fraction in (0.25, -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(fraction=fraction):
                rows = copy.deepcopy(self.rows)
                rows[0]["valid_fraction"] = fraction
                self.assertIn("valid_fraction_denominator", self.check(self.table(rows))["errors"])

    def test_impossible_group_counts_are_rejected(self):
        cases = (
            ({"pixel_count": 0}, "nonpositive_pixel_or_negative_count"),
            ({"finite_temperature_count": -1}, "nonpositive_pixel_or_negative_count"),
            ({"finite_temperature_count": 5}, "count_inequalities"),
            ({"flagged_count": 5}, "count_inequalities"),
            ({"valid_count": 4}, "count_inequalities"),
            ({"flagged_count": 3}, "count_inequalities"),
        )
        for change, error in cases:
            with self.subTest(change=change):
                rows = copy.deepcopy(self.rows)
                rows[0].update(change)
                self.assertIn(error, self.check(self.table(rows))["errors"])

    def test_daily_quality_totals_are_independent_expectations(self):
        for count, source_column in gold.COUNTS.items():
            with self.subTest(count=count):
                expected = {**self.expected, source_column: str(int(self.expected[source_column]) + 1)}
                self.assertIn("quality_total_" + count, self.check(expected=expected)["errors"])

    def test_scene_quality_mismatch_cannot_hide_behind_matching_daily_totals(self):
        for count, source_column in gold.COUNTS.items():
            with self.subTest(count=count):
                scenes = copy.deepcopy(self.scenes)
                scenes[0][source_column] = str(int(scenes[0][source_column]) + 1)
                scenes[1][source_column] = str(int(scenes[1][source_column]) - 1)
                errors = self.check(scenes=scenes)["errors"]
                self.assertIn("scene_sum_A2024001.1600_" + count, errors)
                self.assertIn("scene_sum_A2024001.1800_" + count, errors)
                self.assertNotIn("quality_total_" + count, errors)

    def test_unexpected_swath_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[2]["swath_id"] = "A2024001.2000"
        self.assertIn("unexpected_swath", self.check(self.table(rows))["errors"])

    def test_expected_zero_land_scene_does_not_require_invented_gold_rows(self):
        scenes = self.scenes + [{"swath_id": "A2024001.2000", "time_start_utc": "2024-01-01T20:00:00Z", "pixel_count": "0",
                                 "finite_temperature": "0", "flagged": "0", "valid_flag_zero_temperature": "0"}]
        self.assertEqual(self.check(scenes=scenes)["errors"], [])

    def test_units_must_explicitly_declare_kelvin_and_tai93(self):
        for units in ("K", "Celsius; UTC seconds", ""):
            with self.subTest(units=units):
                metadata = {**self.metadata, "units": units}
                self.assertIn("metadata_units", self.check(self.table(metadata=metadata))["errors"])

    def test_metadata_lineage_must_match_daily_expectation(self):
        for name in ("pipeline_version", "boundary_version", "date_kst", "input_fingerprint",
                     "processing_code_sha256", "boundary_sha256", "source_silver_sha256", "source_silver"):
            with self.subTest(name=name):
                metadata = {**self.metadata, name: "wrong"}
                self.assertIn("metadata_" + name, self.check(self.table(metadata=metadata))["errors"])

    def test_valid_count_cannot_be_smaller_than_finite_minus_flagged(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(valid_count=1, valid_fraction=0.25)
        self.assertIn("count_inequalities", self.check(self.table(rows))["errors"])

    def test_scene_start_must_match_explicit_utc_expectation(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["swath_start_utc"] = datetime(2024, 1, 1, 15, tzinfo=timezone.utc)
        self.assertIn("swath_start_utc_A2024001.1600", self.check(self.table(rows))["errors"])

    def inventory_rows(self):
        common = {"date_kst": self.expected["date_kst"], "boundary_version": self.expected["boundary_version"],
                  "size_bytes": "100", "etag": "0123456789abcdef0123456789abcdef"}
        objects = [{**common, "tier": "gold", "object_key": self.expected["gold_key"], "sha256": "e" * 64},
                   {**common, "tier": "silver", "object_key": self.expected["silver_key"], "sha256": "d" * 64}]
        return objects, [copy.deepcopy(self.expected)], copy.deepcopy(self.scenes)

    @staticmethod
    def write_inventory(directory, objects, days, scenes):
        for name, rows in (("objects.csv", objects), ("days.csv", days), ("scenes.csv", scenes)):
            with (directory / name).open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)

    def test_incomplete_inventory_fails_before_credentials_or_network(self):
        changes = (("sha256", ""), ("sha256", "not-a-digest"), ("size_bytes", "0"), ("etag", ""))
        for field, value in changes:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                objects, days, scenes = self.inventory_rows()
                objects[0][field] = value
                self.write_inventory(directory, objects, days, scenes)
                with patch("boto3.client") as client, patch("dotenv.dotenv_values") as credentials, \
                        patch.object(gold, "verified_gold_path") as byte_read, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        gold.main(["--inventory", str(directory), "--out", str(directory / "out")])
                    self.assertEqual(raised.exception.code, 2)
                    client.assert_not_called()
                    credentials.assert_not_called()
                    byte_read.assert_not_called()

    def test_missing_expected_metadata_fails_preflight(self):
        for field in ("boundary_sha256", "processing_code_sha256", "input_fingerprint", "gold_rows_recorded_server"):
            with self.subTest(field=field):
                objects, days, scenes = self.inventory_rows()
                days[0][field] = ""
                with self.assertRaises(ValueError):
                    gold.validate_inventory(objects, days, scenes)

    def test_preflight_checks_all_objects_before_any_gold_work(self):
        objects, days, scenes = self.inventory_rows()
        objects.append({**objects[0], "object_key": "bronze/bad.nc", "tier": "bronze", "sha256": ""})
        with self.assertRaisesRegex(ValueError, "sha256"):
            gold.validate_inventory(objects, days, scenes)

    def test_no_paired_cmr_day_does_not_require_gold_expectations(self):
        objects, days, scenes = self.inventory_rows()
        days.append({"date_kst": "2024-01-03", "server_status": "NO_PAIRED_CMR"})
        selected, expectations, grouped = gold.validate_inventory(objects, days, scenes)
        self.assertEqual(len(selected), 1)
        self.assertEqual(set(expectations), {"2024-01-02"})
        self.assertEqual(expectations["2024-01-02"]["source_silver_sha256"], "d" * 64)

    def test_content_cache_is_rehashed_before_use_and_corruption_is_replaced(self):
        content = b"known-gold-bytes"
        obj = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": str(len(content))}
        with tempfile.TemporaryDirectory() as tmp:
            body = Mock()
            body.iter_chunks.return_value = [content]
            fetch = Mock(return_value={"Body": body})
            path, reused = gold.verified_gold_path(obj, Path(tmp), fetch)
            self.assertFalse(reused)
            body.close.assert_called_once()
            fetch.reset_mock()
            _, reused = gold.verified_gold_path(obj, Path(tmp), fetch)
            self.assertTrue(reused)
            fetch.assert_not_called()
            path.write_bytes(b"x" * len(content))  # Same size: only the full hash reveals corruption.
            _, reused = gold.verified_gold_path(obj, Path(tmp), fetch)
            self.assertFalse(reused)
            fetch.assert_called_once()
            self.assertEqual(path.read_bytes(), content)

    def test_bad_download_never_enters_byte_cache(self):
        expected = b"trusted"
        obj = {"sha256": hashlib.sha256(expected).hexdigest(), "size_bytes": str(len(expected))}
        body = Mock()
        body.iter_chunks.return_value = [b"altered"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                gold.verified_gold_path(obj, Path(tmp), Mock(return_value={"Body": body}))
            self.assertEqual(list(Path(tmp).glob("*.parquet")), [])
            body.close.assert_called_once()

    def test_interrupted_stream_retries_without_caching_partial_bytes(self):
        from botocore.exceptions import ResponseStreamingError
        content = b"trusted-complete-gold"
        obj = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": str(len(content))}
        broken, good = Mock(), Mock()
        broken.iter_chunks.side_effect = ResponseStreamingError(error=OSError("connection lost"))
        good.iter_chunks.return_value = [content]
        fetch = Mock(side_effect=[{"Body": broken}, {"Body": good}])
        with tempfile.TemporaryDirectory() as tmp, patch.object(gold.time, "sleep") as sleep:
            path, reused = gold.verified_gold_path(obj, Path(tmp), fetch)
            self.assertFalse(reused)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(fetch.call_count, 2)
            broken.close.assert_called_once()
            good.close.assert_called_once()
            sleep.assert_called_once_with(1)
            self.assertEqual(len(list(Path(tmp).glob("*.parquet"))), 1)

    def test_stream_retry_is_bounded_and_never_accepts_partial_file(self):
        from botocore.exceptions import ResponseStreamingError
        content = b"expected"
        obj = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": str(len(content))}
        fetch = Mock(side_effect=ResponseStreamingError(error=OSError("unavailable")))
        with tempfile.TemporaryDirectory() as tmp, patch.object(gold.time, "sleep") as sleep:
            with self.assertRaises(ResponseStreamingError):
                gold.verified_gold_path(obj, Path(tmp), fetch)
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(tmp).glob("*.parquet")), [])

    def test_complete_byte_cache_can_be_rechecked_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cache = directory / "cache"
            cache.mkdir()
            staged = directory / "fixture.parquet"
            pq.write_table(self.table(), staged)
            content = staged.read_bytes()
            obj_sha = hashlib.sha256(content).hexdigest()
            objects, days, scenes = self.inventory_rows()
            objects[0].update(sha256=obj_sha, size_bytes=str(len(content)))
            self.write_inventory(directory, objects, days, scenes)
            (cache / (obj_sha + ".parquet")).write_bytes(content)
            with patch("boto3.client") as client, patch("dotenv.dotenv_values") as credentials, \
                    patch("builtins.print"):
                self.assertEqual(gold.main(["--inventory", str(directory), "--out", str(directory / "out"),
                                            "--cache-dir", str(cache)]), 0)
                client.assert_not_called()
                credentials.assert_not_called()

    def test_temperature_statistics_are_null_exactly_when_valid_count_is_zero(self):
        for name in ("temperature_min_k", "temperature_mean_k", "temperature_max_k"):
            for row_index, value in ((0, None), (1, 0.0)):
                with self.subTest(column=name, row=row_index):
                    rows = copy.deepcopy(self.rows)
                    rows[row_index][name] = value
                    self.assertIn("null_rule_" + name, self.check(self.table(rows))["errors"])

    def test_nonfinite_kelvin_is_rejected_for_valid_groups(self):
        for name in ("temperature_min_k", "temperature_mean_k", "temperature_max_k"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(column=name, value=value):
                    rows = copy.deepcopy(self.rows)
                    rows[0][name] = value
                    self.assertIn("nonfinite_" + name, self.check(self.table(rows))["errors"])

    def test_nonpositive_kelvin_and_statistic_order_are_rejected(self):
        for change, error in (({"temperature_min_k": 0.0}, "nonpositive_kelvin"),
                              ({"temperature_max_k": -1.0}, "nonpositive_kelvin"),
                              ({"temperature_min_k": 282.0}, "min_mean_max_order"),
                              ({"temperature_max_k": 280.0}, "min_mean_max_order")):
            with self.subTest(change=change):
                rows = copy.deepcopy(self.rows)
                rows[0].update(change)
                self.assertIn(error, self.check(self.table(rows))["errors"])

    def test_schema_and_required_nulls_raise_valueerror(self):
        wrong_schema = self.table().set_column(7, "pixel_count", pa.array([4, 3, 5], type=pa.int32()))
        with self.assertRaisesRegex(ValueError, "schema"):
            self.check(wrong_schema)
        rows = copy.deepcopy(self.rows)
        rows[0]["swath_start_utc"] = None
        with self.assertRaisesRegex(ValueError, "Null"):
            self.check(self.table(rows))

    def test_scan_times_must_be_finite_positive_and_ordered(self):
        for change in ({"first_scan_start_time_tai93_s": float("nan")},
                       {"last_scan_start_time_tai93_s": float("inf")},
                       {"first_scan_start_time_tai93_s": 0.0},
                       {"last_scan_start_time_tai93_s": 1.0}):
            with self.subTest(change=change):
                rows = copy.deepcopy(self.rows)
                rows[0].update(change)
                self.assertIn("scan_time_order_or_units", self.check(self.table(rows))["errors"])


if __name__ == "__main__":
    unittest.main()
