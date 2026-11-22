"""Regressions for read-only audit/recovery diagnostics (no R2/NASA calls)."""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import viirs_missing_repair as repair
import viirs_r2_audit as audit


class Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.requests = []

    def add(self, key, data, metadata):
        self.objects[key] = {"data": data, "Metadata": {
            **metadata, "sha256": base64.b64encode(hashlib.sha256(data).digest()).decode()},
            "ETag": hashlib.md5(data).hexdigest(), "ContentLength": len(data)}

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise AssertionError(name)
        return self

    def paginate(self, Bucket, Prefix):
        self.requests.append("LIST")
        yield {"Contents": [{"Key": key, "Size": obj["ContentLength"], "ETag": obj["ETag"]}
                            for key, obj in self.objects.items() if key.startswith(Prefix)]}

    def head_object(self, Bucket, Key):
        self.requests.append("HEAD")
        return {key: value for key, value in self.objects[Key].items() if key != "data"}

    def get_object(self, Bucket, Key, **options):
        self.requests.append("GET")
        obj = self.objects[Key]
        if options.get("IfMatch") != obj["ETag"]:
            raise AssertionError("GET must be pinned to inspected ETag")
        return {"Body": Body(obj["data"])}


def daily_fixture():
    client = FakeR2()
    base = "sgis-2024-2q/2024/01/03/"
    scenes = ["A2024003.0306", "A2024003.0448"]
    md = {"pipeline-version": "all-scenes-scan-time-v2.1", "input-fingerprint": "a" * 64}
    for scene in scenes:
        for product in ("VNP02IMG", "VNP03IMG"):
            name = f"{product}.{scene}.002.2024003000000.nc"
            client.add("viirs-daily/bronze/2024/01/03/" + name, b"source", {"nasa-granule": name})
    with tempfile.TemporaryDirectory() as td:
        for kind, names in (("silver", ["dong_code", "row", "column", "longitude", "latitude",
                                      "i05_brightness_temperature_k", "i05_quality_flags", "swath_id",
                                      "swath_start_utc", "scan_index", "scan_start_time_tai93_s"]),
                            ("gold", ["dong_code", "swath_id", "swath_start_utc", "pixel_count", "valid_count"])):
            dest = Path(td) / f"{kind}.parquet"
            # Empty observations still need a typed schema and valid Parquet footer.
            schema = pa.schema([(name, pa.int64() if name.endswith("count") else pa.string()) for name in names])
            pq.write_table(pa.Table.from_pylist([], schema=schema), dest)
            client.add(f"viirs-daily/{kind}/" + base + audit.DAILY_FILES[kind], dest.read_bytes(), md)
    quality = {"date_kst": "2024-01-03", "boundary_version": "sgis-2024-2q", "pixel_count": 0,
               "source_scene_count": 2, "per_scene": {scene: {"pixel_count": 0} for scene in scenes},
               "status": "EMPTY_LAND_OBSERVATION", "valid_flag_zero_temperature": 0}
    client.add("viirs-daily/quality/" + base + audit.DAILY_FILES["quality"], json.dumps(quality).encode(), md)
    manifest = {"manifest_version": 1, "pipeline_version": md["pipeline-version"], "date_kst": "2024-01-03",
                "boundary_version": "sgis-2024-2q", "input_fingerprint": md["input-fingerprint"], "objects": {}}
    for kind, name in audit.DAILY_FILES.items():
        key = f"viirs-daily/{kind}/" + base + name
        obj = client.objects[key]
        manifest["objects"][kind] = {"key": key, "size_bytes": obj["ContentLength"], "sha256": obj["Metadata"]["sha256"]}
    client.add("viirs-daily/quality/" + base + "completion.json", json.dumps(manifest).encode(), md)
    return client


class AuditTests(unittest.TestCase):
    def run_audit(self, client, deep=True):
        with tempfile.TemporaryDirectory() as td:
            args = SimpleNamespace(env="/does/not/exist", prefix="viirs-daily", years=[2024],
                                   start=date(2024, 1, 3), stop=date(2024, 1, 4), boundary_2024="sgis-2024-2q",
                                   boundary_2025="sgis-2025-2q", workers=2, deep=deep, deep_bronze=False, out=td)
            with patch.object(audit, "make_client", return_value=(client, "test")), redirect_stdout(io.StringIO()):
                status = audit.audit(args)
            summary = json.loads((Path(td) / "summary.json").read_text())
            issues = (Path(td) / "issues.csv").read_text(encoding="utf-8-sig")
            return status, summary, issues

    def test_daily_multiple_pairs_and_empty_land_are_valid(self):
        client = daily_fixture()
        code, summary, issues = self.run_audit(client)
        self.assertEqual(code, 0, issues)
        self.assertEqual(summary["date_status_counts"], {"PASS": 1})
        self.assertTrue(set(client.requests) <= {"LIST", "HEAD", "GET"})

    def test_missing_publication_is_unverified_and_stale_is_failure(self):
        client = daily_fixture()
        marker_key = next(key for key in client.objects if key.endswith("completion.json"))
        marker = client.objects.pop(marker_key)
        _, summary, _ = self.run_audit(client)
        self.assertEqual(summary["date_status_counts"], {"UNVERIFIED_PUBLICATION": 1})
        client.objects[marker_key] = marker
        silver = next(obj for key, obj in client.objects.items() if key.endswith("all-observations.parquet"))
        silver["Metadata"]["input-fingerprint"] = "b" * 64
        code, summary, issues = self.run_audit(client)
        self.assertEqual(code, 2)
        self.assertEqual(summary["date_status_counts"], {"FAIL": 1})
        self.assertIn("PUBLICATION_FAILED", issues)

    def test_base64_hash_mismatch_is_not_silently_accepted(self):
        client = daily_fixture()
        key = next(key for key in client.objects if key.endswith("all-observations.json"))
        obj = client.objects[key]
        obj["Metadata"]["sha256"] = base64.b64encode(b"x" * 32).decode()
        parsed = {"Key": key, "Size": obj["ContentLength"], "ETag": obj["ETag"], "meta": obj["Metadata"]}
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            audit.content_check(client, "test", parsed, "quality")

    def test_incomplete_original_pair_is_failure(self):
        client = daily_fixture()
        client.objects.pop(next(key for key in client.objects if "VNP03IMG" in key))
        code, summary, issues = self.run_audit(client)
        self.assertEqual(code, 2)
        self.assertIn("BRONZE_PAIRING", issues)

    def test_legacy_scene_outputs_remain_auditable(self):
        client = daily_fixture()
        scene = "A2024003.0306"
        bronze = {key: obj for key, obj in client.objects.items() if "/bronze/" in key and scene in key}
        source = next(obj for key, obj in client.objects.items() if key.endswith("all-observations.parquet"))
        client.objects = bronze
        md = {"swath": scene, "boundary-sha256": "c" * 64,
              "i05-granule": next(Path(key).name for key in bronze if "VNP02IMG" in key),
              "geolocation-granule": next(Path(key).name for key in bronze if "VNP03IMG" in key)}
        client.add(f"viirs-daily/silver/sgis-2024-2q/2024/01/03/{scene}.parquet", source["data"], md)
        client.add(f"viirs-daily/quality/sgis-2024-2q/2024/01/03/{scene}.json",
                   b'{"pixel_count":0}', {**md, "report-version": "1"})
        code, summary, issues = self.run_audit(client)
        self.assertEqual(code, 0, issues)
        self.assertEqual(summary["date_status_counts"], {"PASS": 1})


class DiagnosticTests(unittest.TestCase):
    def pipeline(self, unpaired=False):
        scenes = ["A2024002.1618", "A2024003.0306", "A2024003.0448", "A2024003.1618"]
        # Jan 3 KST starts Jan 2 15:00 UTC and stops Jan 3 15:00 UTC.
        def search(session, product, start, stop):
            return {scene: ("name", "url") for scene in scenes if not (unpaired and product == "VNP03IMG" and scene.endswith("0448"))}
        return SimpleNamespace(KST=timezone(timedelta(hours=9)), PRODUCTS=("VNP02IMG", "VNP03IMG"),
                               search=search, timestamp=lambda key: datetime.strptime(key, "A%Y%j.%H%M").replace(tzinfo=timezone.utc))

    def test_cmr_reports_all_scene_pairs_and_kst_boundary(self):
        result = repair.one_day_cmr(self.pipeline(), None, date(2024, 1, 3))
        self.assertEqual(result["cmr_pairs"], 3)
        self.assertEqual(result["swaths"], ["A2024002.1618", "A2024003.0306", "A2024003.0448"])
        result = repair.one_day_cmr(self.pipeline(True), None, date(2024, 1, 3))
        self.assertEqual(result["status"], "CMR_UNPAIRED_PRODUCTS")
        self.assertEqual(result["cmr_pairs"], 2)

    def test_legacy_apply_is_rejected_before_any_network(self):
        with patch.object(repair, "arguments", return_value=SimpleNamespace(apply=True)):
            with self.assertRaisesRegex(ValueError, "--apply is disabled"):
                repair.main()


if __name__ == "__main__":
    unittest.main()
