"""Offline tests for leakage prevention, missing data, metrics and CNN execution."""
import csv
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location("rain_train", Path(__file__).with_name("train.py"))
train = importlib.util.module_from_spec(spec)
spec.loader.exec_module(train)


class ForecastTrainingTests(unittest.TestCase):
    def test_numeric_imputation_is_fitted_only_on_train(self):
        values = np.array([[10, np.nan], [20, 2]], dtype=float)
        state = train.fit_preprocessor(values)
        result = train.transform_numeric(np.array([[1000, np.nan]]), state)
        self.assertEqual(state["median"], [15.0, 2.0])
        self.assertEqual(state["mean"], [15.0, 2.0])
        self.assertEqual(result[0, 0], 197.0)
        self.assertEqual(result[0, -1], 1.0)

    def test_numeric_target_is_not_used_as_past_rain(self):
        rows = train.numeric_rows([{"rain_1h_mm": "50", "wind_direction_deg": "90"}])
        self.assertTrue(np.isnan(rows[0, -1]))
        self.assertAlmostEqual(rows[0, 4], 1.0)

    def test_average_precision_handles_ties_and_single_class(self):
        result = train.classification_metrics([0, 1, 0, 1], [0.5] * 4)
        self.assertEqual(result["average_precision"], 0.5)
        self.assertEqual(result["confusion"], {"tp": 2, "fp": 2, "fn": 0, "tn": 0})
        self.assertIsNone(train.classification_metrics([0, 0], [0.2, 0.3])["average_precision"])
        with self.assertRaises(ValueError):
            train.select_threshold([0, 0], [0.2, 0.3])

    def test_threshold_selection_uses_supplied_validation_only(self):
        self.assertEqual(train.select_threshold([0, 1, 1], [0.1, 0.7, 0.9]), 0.7)

    def make_samples(self, folder):
        frame = folder / "patch.npz"
        np.savez_compressed(frame, dn=np.full((16, 16), 4096, np.uint16), valid=np.ones((16, 16), np.uint8))
        sha = train.digest(frame)
        rows = []
        for split, issue in [("train", datetime(2024, 1, 1, tzinfo=timezone.utc)),
                             ("val", datetime(2025, 1, 1, tzinfo=timezone.utc)),
                             ("test", datetime(2025, 7, 1, tzinfo=timezone.utc))]:
            for index, rain in enumerate((0, 30)):
                when = issue + timedelta(hours=index)
                rows.append({"sample_id": split + str(index), "station_id": "108", "split": split,
                             "issue_time_kst": when.isoformat(), "target_start_kst": when.isoformat(),
                             "target_end_kst": (when + timedelta(hours=1)).isoformat(),
                             "label": int(rain >= 30), "rain_1h_mm": rain,
                             "frame_paths": json.dumps([frame.name] * 4), "frame_sha256": json.dumps([sha] * 4),
                             "frame_times_utc": json.dumps([(when - timedelta(hours=n)).isoformat() for n in (3, 2, 1, 0)])})
        path = folder / "samples.csv"
        self.write_rows(path, rows)
        return path, rows

    @staticmethod
    def write_rows(path, rows):
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def test_real_samples_validate_labels_times_and_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, rows = self.make_samples(Path(temporary))
            groups = train.read_samples(path, min_class_samples=1)
            self.assertEqual(len(groups["train"]), 2)
            rows[0]["label"] = 1
            self.write_rows(path, rows)
            with self.assertRaisesRegex(ValueError, "Rain label"):
                train.read_samples(path, min_class_samples=1)

    def test_future_image_and_damaged_patch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path, rows = self.make_samples(folder)
            rows[0]["frame_times_utc"] = rows[1]["frame_times_utc"]
            self.write_rows(path, rows)
            with self.assertRaisesRegex(ValueError, "Image history"):
                train.read_samples(path, min_class_samples=1)
            path, rows = self.make_samples(folder)
            (folder / "patch.npz").write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "checksum"):
                train.read_samples(path, min_class_samples=1)

    def test_missing_class_and_split_overlap_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, rows = self.make_samples(Path(temporary))
            with self.assertRaisesRegex(ValueError, "class counts"):
                train.read_samples(path, min_class_samples=2)
            origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
            for index, row in enumerate(rows[2:4]):
                issue = origin + timedelta(hours=3 + index)
                row.update(issue_time_kst=issue.isoformat(), target_start_kst=issue.isoformat(),
                           target_end_kst=(issue + timedelta(hours=1)).isoformat(),
                           frame_times_utc=json.dumps([(issue - timedelta(hours=n)).isoformat() for n in (3, 2, 1, 0)]))
            self.write_rows(path, rows)
            with self.assertRaisesRegex(ValueError, "Temporal splits overlap"):
                train.read_samples(path, min_class_samples=1)

    def test_dn_mask_preserves_missing_without_kelvin_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            frame = Path(temporary) / "image.npz"
            np.savez(frame, dn=np.array([[8191, 65535]], np.uint16), valid=np.array([[1, 0]], np.uint8))
            result = train.image_stack([frame] * 4)
            self.assertEqual(result.shape, (8, 1, 2))
            self.assertEqual(result[0, 0].tolist(), [1.0, 0.0])
            self.assertEqual(result[4, 0].tolist(), [1.0, 0.0])

    @unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch is installed in the isolated CNN environment")
    def test_cnn_backward_and_checkpoint_roundtrip_are_synthetic(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = train.smoke_test(Path(temporary) / "smoke")
            self.assertTrue(result["synthetic"])
            self.assertTrue(result["checkpoint_roundtrip_equal"])
            self.assertEqual(result["optimization_steps"], 2)
            self.assertNotIn("test", result)


if __name__ == "__main__":
    unittest.main()
