"""Offline regressions for rebuild publication, source integrity and scene timing."""
import base64
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from viirs_gold import inventory

SPEC = importlib.util.spec_from_file_location(
    "viirs_full_rebuild", Path(__file__).parent / "viirs-full-rebuild-hetzner/viirs_full_rebuild.py")
rebuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebuild)


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.race_body = None

    def store(self, key, body, metadata):
        self.objects[key] = (body, dict(metadata))

    def head_object(self, *, Key, **kwargs):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"},
                               "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        body, metadata = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": metadata.copy(),
                "ETag": '"' + hashlib.md5(body).hexdigest() + '"'}

    def put_object(self, *, Key, Body, Metadata, **kwargs):
        if self.race_body is not None:
            body = self.race_body
            self.race_body = None
            self.store(Key, body, {**Metadata, "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode()})
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.store(Key, Body.read(), Metadata)

    def get_object(self, *, Key, **kwargs):
        body = self.objects[Key][0]
        return {"Body": StreamingBody(io.BytesIO(body), len(body))}

    def get_paginator(self, operation):
        return SimpleNamespace(paginate=lambda **kwargs: [{"Contents": [
            {"Key": key} for key in self.objects if key.startswith(kwargs["Prefix"])]}])

    def delete_object(self, *, Key, **kwargs):
        self.objects.pop(Key, None)


class RebuildIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore()

    def test_concurrent_different_bronze_is_rejected(self):
        path = self.root / "source.nc"
        path.write_bytes(b"expected original")
        self.store.race_body = b"different original"
        with self.assertRaisesRegex(RuntimeError, "Concurrent publication"):
            rebuild.upload_verified(self.store, "bucket", "bronze/source.nc", path,
                                    {"nasa-granule": "source.nc"}, "application/x-netcdf", replace=False)
        self.assertEqual(self.store.objects["bronze/source.nc"][0], b"different original")

    def test_existing_bronze_bytes_must_match_stored_checksum(self):
        import viirs_pipeline as pipeline

        filename = "VNP02IMG.A2024001.1600.002.2024002111111.nc"
        key = f"viirs-daily/bronze/2024/01/02/{filename}"
        self.store.store(key, b"damaged", {"nasa-granule": filename,
            "sha256": base64.b64encode(hashlib.sha256(b"original").digest()).decode()})
        with self.assertRaisesRegex(ValueError, "Downloaded object failed verification"):
            rebuild.ensure_bronze(self.store, "bucket", "viirs-daily", "A2024001.1600",
                {pipeline.PRODUCTS[0]: (filename, "unused")}, None, None, pipeline, self.root)

    def test_legacy_cleanup_only_selects_direct_scene_outputs(self):
        paths = rebuild.daily_paths("viirs-daily", "sgis-2024-2q", date(2024, 1, 2))
        for label, key in paths.items():
            stem = key.rsplit("/", 1)[0]
            suffix = ".json" if label == "quality" else ".parquet"
            for name in (key, f"{stem}/A2024001.1600{suffix}",
                         f"{stem}/custom{suffix}", f"{stem}/nested/A2024001.1600{suffix}"):
                self.store.store(name, b"x", {})
        selected = rebuild.old_keys(self.store, "bucket", paths)
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(key.rsplit("/", 1)[-1].startswith("A2024001.1600") for key in selected))

    def test_completion_manifest_matches_all_three_published_objects(self):
        paths = rebuild.daily_paths("viirs-daily", "sgis-2024-2q", date(2024, 1, 2))
        metadata = {"pipeline-version": rebuild.VERSION, "input-fingerprint": "abc"}
        for label, key in paths.items():
            path = self.root / label
            path.write_bytes(label.encode())
            rebuild.upload_verified(self.store, "bucket", key, path, metadata,
                                    "application/octet-stream", replace=True)
        rebuild.publish_completion(self.store, "bucket", paths, date(2024, 1, 2),
                                   "sgis-2024-2q", "abc", metadata, self.root)
        key = paths["quality"].rsplit("/", 1)[0] + "/completion.json"
        manifest = json.loads(self.store.objects[key][0])
        self.assertEqual(set(manifest["objects"]), {"silver", "quality", "gold"})
        for label, item in manifest["objects"].items():
            self.assertEqual(item["key"], paths[label])
            self.assertEqual(item["size_bytes"], len(label))
            self.assertEqual(item["sha256"], base64.b64encode(hashlib.sha256(label.encode()).digest()).decode())
        self.store.objects.pop(paths["gold"])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            rebuild.publish_completion(self.store, "bucket", paths, date(2024, 1, 2),
                                       "sgis-2024-2q", "abc", metadata, self.root)

    def test_scan_start_mapping_rejects_missing_and_incompatible_scan_counts(self):
        source = SimpleNamespace(groups={})
        with self.assertRaisesRegex(ValueError, "scan_line_attributes"):
            rebuild.scan_times(source, 32)
        times = np.ma.array([1000., 1001.])
        class ScanGroup:
            variables = {"scan_start_time": times}
            def __getitem__(self, name):
                return self.variables[name]
        source.groups["scan_line_attributes"] = ScanGroup()
        actual, lines = rebuild.scan_times(source, 32)
        self.assertEqual(lines, 16)
        np.testing.assert_array_equal(actual, times)
        with self.assertRaisesRegex(ValueError, "Cannot map"):
            rebuild.scan_times(source, 31)

    def test_daily_gold_keeps_separate_swaths_and_source_checksum(self):
        source, output = self.root / "silver.parquet", self.root / "gold.parquet"
        rows = []
        for scene, value, flag in (("A2024001.1600", 280., 0), ("A2024001.1600", 999., 1),
                                   ("A2024001.1606", 290., 0)):
            rows.append({"dong_code": "11010530", "swath_id": scene,
                         "swath_start_utc": datetime(2024, 1, 1, 16, int(scene[-2:]), tzinfo=timezone.utc),
                         "scan_start_time_tai93_s": 1000., "i05_brightness_temperature_k": value,
                         "i05_quality_flags": flag})
        pq.write_table(pa.Table.from_pylist(rows, schema=rebuild.SILVER_SCHEMA), source)
        self.assertEqual(rebuild.build_gold(source, output, date(2024, 1, 2), "sgis-2024-2q", "silver-key"), 2)
        result = pq.read_table(output)
        self.assertEqual([row["temperature_mean_k"] for row in result.to_pylist()], [280., 290.])
        self.assertEqual(result.schema.metadata[b"source_silver_sha256"].decode(), hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(result.schema.metadata[b"cloud_qc_status"], b"unverified")

    def test_legacy_gold_inventory_skips_current_daily_files(self):
        prefix = "viirs-daily/silver/sgis-2024-2q/2024/01/02/"
        self.store.store(prefix + "all-observations.parquet", b"x", {})
        self.store.store(prefix + "A2024001.1600.parquet", b"x", {})
        self.assertEqual(inventory(self.store, "bucket", "viirs-daily", date(2024, 1, 1),
                                   date(2024, 1, 3)), [prefix + "A2024001.1600.parquet"])

    def test_native_extraction_preserves_nulls_flags_scan_start_and_declared_units(self):
        import geopandas as gpd
        from netCDF4 import Dataset
        from shapely.geometry import box
        import viirs_pipeline as pipeline

        scene = "A2024001.1600"
        image = self.root / f"VNP02IMG.{scene}.002.2024002111111.nc"
        geo = self.root / f"VNP03IMG.{scene}.002.2024002111111.nc"
        for path in (image, geo):
            with Dataset(path, "w") as source:
                source.time_coverage_start = "2024-01-01T16:00:01Z"
                source.createDimension("line", 32)
                source.createDimension("column", 2)
                if path == image:
                    group = source.createGroup("observation_data")
                    band = group.createVariable("I05", "u2", ("line", "column"), fill_value=65535)
                    band[:] = np.zeros((32, 2), dtype=np.uint16)
                    band[0, 0] = 65535
                    flags = group.createVariable("I05_quality_flags", "u2", ("line", "column"))
                    flags[:] = np.zeros((32, 2), dtype=np.uint16)
                    flags[0, 0] = 256
                    source.createDimension("lookup", 1)
                    lut = group.createVariable("I05_brightness_temperature_lut", "f4", ("lookup",))
                    lut.units = "K"
                    lut[:] = [280.]
                else:
                    group = source.createGroup("geolocation_data")
                    group.createVariable("longitude", "f4", ("line", "column"))[:] = 127.
                    group.createVariable("latitude", "f4", ("line", "column"))[:] = 37.
                    scan = source.createGroup("scan_line_attributes")
                    source.createDimension("scan", 2)
                    times = scan.createVariable("scan_start_time", "f8", ("scan",))
                    times.units = "seconds since 1993-01-01 00:00:00 TAI"
                    times[:] = [1000., 1001.]
        dongs = gpd.GeoDataFrame({"geometry": [box(126, 36, 128, 38)]},
                                 index=["11010530"], crs=4326)
        output = self.root / "native.parquet"
        with pq.ParquetWriter(output, rebuild.SILVER_SCHEMA) as writer:
            result = rebuild.extract_scene(image, geo, scene, dongs, writer, pipeline)
        self.assertEqual(result["pixel_count"], 64)
        self.assertEqual(result["finite_temperature"], 63)
        self.assertEqual(result["valid_flag_zero_temperature"], 63)
        self.assertEqual(result["source_metadata"]["products"]["VNP02IMG"]["variables"][
            "observation_data/I05_brightness_temperature_lut"]["units"], "K")
        rows = pq.read_table(output).to_pylist()
        self.assertIsNone(rows[0]["i05_brightness_temperature_k"])
        self.assertEqual(rows[0]["i05_quality_flags"], 256)
        self.assertEqual(rows[-1]["scan_index"], 1)
        self.assertEqual(rows[-1]["scan_start_time_tai93_s"], 1001.)

    def test_complete_day_publication_and_resume_are_idempotent(self):
        import viirs_pipeline as pipeline

        scene = "A2024001.1600"
        day = date(2024, 1, 2)
        paths = rebuild.daily_paths("viirs-daily", "sgis-2024-2q", day)
        old = paths["silver"].rsplit("/", 1)[0] + f"/{scene}.parquet"
        self.store.store(old, b"legacy", {})
        def bronze(*args):
            files = {}
            for product in pipeline.PRODUCTS:
                path = self.root / f"{product}.nc"
                path.write_bytes(product.encode())
                files[product] = path
            return files, ["SKIPPED_EXISTING"] * 2
        def extract(image, geo, swath, dongs, writer, pipeline):
            row = {"dong_code": "11010530", "row": 0, "column": 0, "longitude": 127., "latitude": 37.,
                   "i05_brightness_temperature_k": 280., "i05_quality_flags": 0, "swath_id": swath,
                   "swath_start_utc": pipeline.timestamp(swath), "scan_index": 0,
                   "scan_start_time_tai93_s": 1000.}
            writer.write_table(pa.Table.from_pylist([row], schema=writer.schema))
            return {"pixel_count": 1, "finite_temperature": 1, "flagged": 0,
                    "valid_flag_zero_temperature": 1, "source_metadata": {}}
        assets = {scene: {product: (f"{product}.nc", "unused") for product in pipeline.PRODUCTS}}
        arguments = (self.store, "bucket", "viirs-daily", day, [scene], assets, None,
                     Path("boundary.shp"), "sgis-2024-2q", "boundary-digest", "code-digest",
                     None, None, pipeline, self.root)
        with patch.object(rebuild, "ensure_bronze", side_effect=bronze), patch.object(
                rebuild, "extract_scene", side_effect=extract) as extraction:
            first = rebuild.one_day(*arguments)
            second = rebuild.one_day(*arguments)
            self.assertEqual(extraction.call_count, 1)
        self.assertEqual(first["pixels"], 1)
        self.assertEqual(first["obsolete_deleted"], 1)
        self.assertEqual(second["status"], "SKIPPED_VERIFIED_EXISTING")
        self.assertNotIn(old, self.store.objects)
        self.assertEqual(len(self.store.objects), 4)


if __name__ == "__main__":
    unittest.main()
