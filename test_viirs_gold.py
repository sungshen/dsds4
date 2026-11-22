"""Offline regression tests for VIIRS Gold quality, time and source lineage."""
import base64
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from viirs_gold import aggregate_gold
from viirs_s3 import checksum


class GoldAggregationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "silver.parquet"
        self.destination = self.root / "gold.parquet"
        self.key = "viirs-daily/silver/sgis-2024-2q/2024/01/02/A2024001.1600.parquet"
        self.rows = [
            ("11010530", 280.0, 0),
            ("11010540", None, 256),
            ("11010530", 300.0, 1),
            ("11010540", 285.0, 2),
            ("11010530", None, 0),
            ("11010530", float("nan"), 0),
            ("11010530", 282.0, 0),
            ("11010540", None, 0),
            ("11010530", float("inf"), 0),
        ]

    def write_source(self, rows=None, **metadata_changes):
        metadata = {
            "swath_start_utc": "2024-01-01T16:00:00+00:00",
            "boundary_version": "sgis-2024-2q",
            "sources": json.dumps([
                "VNP02IMG.A2024001.1600.002.2024002111111.nc",
                "VNP03IMG.A2024001.1600.002.2024002111111.nc",
            ]),
        }
        metadata.update(metadata_changes)
        schema = pa.schema([
            ("dong_code", pa.string()),
            ("i05_brightness_temperature_k", pa.float32()),
            ("i05_quality_flags", pa.uint16()),
        ], metadata={key.encode(): value.encode() for key, value in metadata.items()})
        records = [dict(zip(schema.names, row)) for row in (self.rows if rows is None else rows)]
        pq.write_table(pa.Table.from_pylist(records, schema=schema), self.source)
        return checksum(self.source)[0]

    def aggregate(self, **options):
        return aggregate_gold(
            self.source, self.destination, source_key=self.key,
            source_sha256=checksum(self.source)[0], **options,
        )

    def test_batch_boundaries_preserve_quality_counts_and_null_statistics(self):
        self.write_source()
        self.assertEqual(self.aggregate(batch_size=1), 2)
        first = pq.read_table(self.destination)
        rows = {row["dong_code"]: row for row in first.to_pylist()}
        usable = rows["11010530"]
        self.assertEqual(usable["pixel_count"], 6)
        self.assertEqual(usable["nonnull_count"], 3)
        self.assertEqual(usable["valid_count"], 2)
        self.assertEqual(usable["flagged_count"], 1)
        self.assertEqual(usable["temperature_mean_k"], 281.0)
        self.assertEqual(usable["temperature_min_k"], 280.0)
        self.assertEqual(usable["temperature_max_k"], 282.0)
        self.assertAlmostEqual(usable["valid_fraction"], 2 / 6)
        excluded = rows["11010540"]
        self.assertEqual(excluded["pixel_count"], 3)
        self.assertEqual(excluded["nonnull_count"], 1)
        self.assertEqual(excluded["valid_count"], 0)
        self.assertEqual(excluded["flagged_count"], 2)
        self.assertIsNone(excluded["temperature_mean_k"])
        self.assertIsNone(excluded["temperature_min_k"])
        self.assertIsNone(excluded["temperature_max_k"])
        self.assertEqual(excluded["valid_fraction"], 0)

        other = self.root / "other-batches.parquet"
        aggregate_gold(self.source, other, source_key=self.key,
                       source_sha256=checksum(self.source)[0], batch_size=5)
        self.assertTrue(first.equals(pq.read_table(other), check_metadata=True))

    def test_kst_date_and_actual_observation_identity_are_preserved(self):
        self.write_source()
        self.aggregate()
        for row in pq.read_table(self.destination).to_pylist():
            self.assertEqual(row["date"], date(2024, 1, 2))
            self.assertEqual(row["observation_time_utc"], datetime(2024, 1, 1, 16, tzinfo=timezone.utc))
            self.assertEqual(row["source_observation_id"], "A2024001.1600")
            self.assertEqual(row["boundary_version"], "sgis-2024-2q")

    def test_lineage_and_quality_report_are_embedded(self):
        digest = self.write_source()
        report = {"pixel_count": 9, "dong_count": 3558, "dongs_without_valid_temperature": 3556}
        self.aggregate(quality_report=report)
        metadata = pq.read_schema(self.destination).metadata
        self.assertEqual(metadata[b"cloud_qc_status"], b"unverified")
        self.assertEqual(metadata[b"quality_report_status"], b"available")
        self.assertEqual(json.loads(metadata[b"quality_report"]), report)
        self.assertEqual(metadata[b"source_object_key"].decode(), self.key)
        self.assertEqual(metadata[b"source_sha256"].decode(), digest)

    def test_checksum_mismatch_rejects_source_without_output(self):
        self.write_source()
        with self.assertRaises(ValueError):
            aggregate_gold(self.source, self.destination, source_key=self.key,
                           source_sha256=base64.b64encode(bytes(32)).decode())
        self.assertFalse(self.destination.exists())

    def test_path_date_and_source_metadata_must_agree(self):
        self.write_source()
        invalid_key = self.key.replace("2024/01/02", "2024/01/01")
        with self.assertRaises(ValueError):
            aggregate_gold(self.source, self.destination, source_key=invalid_key,
                           source_sha256=checksum(self.source)[0])
        self.assertFalse(self.destination.exists())

    def test_boundary_and_observation_mismatch_are_rejected(self):
        for changes in (
            {"boundary_version": "sgis-2025-2q"},
            {"swath_start_utc": "2024-01-01T16:06:00+00:00"},
            {"swath_start_utc": "2024-01-01T16:00:00"},
        ):
            with self.subTest(changes=changes):
                self.write_source(**changes)
                with self.assertRaises(ValueError):
                    self.aggregate()
                self.assertFalse(self.destination.exists())

    def test_quality_report_must_match_source_pixel_count(self):
        self.write_source()
        with self.assertRaises(ValueError):
            self.aggregate(quality_report={"pixel_count": 8})
        self.assertFalse(self.destination.exists())

    def test_empty_source_has_typed_output_and_missing_report_status(self):
        self.write_source(rows=[])
        self.assertEqual(self.aggregate(), 0)
        result = pq.read_table(self.destination)
        self.assertEqual(result.num_rows, 0)
        self.assertEqual(result.schema.field("date").type, pa.date32())
        self.assertEqual(result.schema.field("temperature_mean_k").type, pa.float64())
        self.assertEqual(result.schema.metadata[b"quality_report_status"], b"missing")
        self.assertEqual(result.schema.metadata[b"cloud_qc_status"], b"unverified")


if __name__ == "__main__":
    unittest.main()
