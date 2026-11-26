"""Data-integrity checks for NGII handoff generation, without network or raw ZIPs."""
import csv
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("ngii_inventory", HERE / "ngii_delivery_inventory.py")
ngii = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ngii)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.tables = self.repo / "tables/ngii"
        (self.tables / "metadata").mkdir(parents=True)
        self.out = self.repo / "report"
        original = HERE.parent / "tables/ngii"
        self.events = ngii.csv_rows(original / "event_registry.csv", ngii.REQUIRED["event_registry.csv"])
        ngii.write_csv(self.tables / "event_registry.csv", self.events)
        self.meta = ngii.csv_rows(original / "metadata/ard_metadata_2024.csv", ngii.META_REQUIRED)[0]
        self.key = self.meta["catalog_id"]
        self.manifest = next(row for row in ngii.csv_rows(original / "asset_manifest.csv", ngii.REQUIRED["asset_manifest.csv"]) if row["catalog_id"] == self.key)
        ngii.write_csv(self.tables / "asset_manifest.csv", [self.manifest])
        ngii.write_csv(self.tables / "metadata/ard_metadata_2024.csv", [self.meta])
        for name in ("rain_region_dongs.csv", "rain_region_coverage.csv", "sweep_sigungu.csv"):
            rows = ngii.csv_rows(original / name, ngii.REQUIRED[name])
            ngii.write_csv(self.tables / name, [next(row for row in rows if row["event_id"] == "E1")])
        with tarfile.open(original / "metadata/ard_aux_xml_2024.tar.gz") as archive:
            member = next(m for m in archive.getmembers() if Path(m.name).name == self.meta["aux_xml"])
            data = archive.extractfile(member).read()
        with tarfile.open(self.tables / "metadata/ard_aux_xml_2024.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    def test_explicit_utc_fraction_and_day_rollover(self):
        utc, kst = ngii.compact_utc("20241231233000.1234567")
        self.assertEqual(utc, "2024-12-31T23:30:00.1234567Z")
        self.assertEqual(kst, "2025-01-01T08:30:00.1234567+09:00")
        with self.assertRaises(ValueError):
            ngii.compact_utc("2024-12-31 23:30:00 unknown")

    def test_missing_raw_and_uncertain_units_never_become_completed_qc(self):
        result = ngii.build(self.repo, self.out)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["raw_missing_local_count"], 1)
        row = ngii.csv_rows(self.out / "image_inventory.csv", {"catalog_id"})[0]
        self.assertEqual(row["raw_checksum_check_now"], "not_available")
        self.assertEqual(row["own_silver_qc_status"], "not_performed")
        self.assertEqual(row["provider_mask_value_unit"], "unknown_PerPixelPercentage_field_preserved_raw")
        self.assertIn("not_exact_tile_exposure", row["time_reference"])

    def test_duplicate_manifest_fails_and_preserves_existing_summary(self):
        old = self.tables / "event_summary.csv"
        old.write_text("original limited AOI snapshot\n")
        ngii.write_csv(self.tables / "asset_manifest.csv", [self.manifest, self.manifest])
        with self.assertRaisesRegex(ValueError, "validation failed"):
            ngii.build(self.repo, self.out, reconcile=True)
        self.assertEqual(old.read_text(), "original limited AOI snapshot\n")
        result = json.loads((self.out / "validation_summary.json").read_text())
        self.assertGreater(result["error_count"], 0)

    def test_schema_failure_is_not_silently_filled(self):
        (self.tables / "asset_manifest.csv").write_text("catalog_id,event_id\nitem,E1\n")
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            ngii.build(self.repo, self.out)

    def test_metadata_xml_mismatch_is_detected(self):
        self.meta["width"] = "999"
        ngii.write_csv(self.tables / "metadata/ard_metadata_2024.csv", [self.meta])
        result = ngii.build(self.repo, self.out)
        self.assertGreater(result["error_count"], 0)
        findings = ngii.csv_rows(self.out / "validation_findings.csv", {"check"})
        self.assertTrue(any(r["check"] == "metadata_xml_value_mismatch" and r["detail"] == "width" for r in findings))

    def test_available_raw_checksum_is_actually_verified(self):
        raw = self.repo / self.manifest["stored_path"]
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"raw-zip-fixture")
        self.manifest["size_bytes"] = str(raw.stat().st_size)
        self.manifest["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.meta["zip_sha256"] = self.manifest["sha256"]
        ngii.write_csv(self.tables / "asset_manifest.csv", [self.manifest])
        ngii.write_csv(self.tables / "metadata/ard_metadata_2024.csv", [self.meta])
        result = ngii.build(self.repo, self.out, verify_raw=True)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(ngii.csv_rows(self.out / "image_inventory.csv", {"catalog_id"})[0]["raw_checksum_check_now"], "match")
        raw.write_bytes(b"bad-zip-fixture")
        result = ngii.build(self.repo, self.out, verify_raw=True)
        self.assertGreater(result["error_count"], 0)

    def test_reconcile_preserves_initial_snapshot_once(self):
        old = self.tables / "event_summary.csv"
        old.write_text("old initial AOI\n")
        ngii.build(self.repo, self.out, reconcile=True)
        archived = self.tables / "event_summary_initial_aoi.csv"
        self.assertEqual(archived.read_text(), "old initial AOI\n")
        ngii.build(self.repo, self.out, reconcile=True)
        self.assertEqual(archived.read_text(), "old initial AOI\n")

    def test_shared_image_links_do_not_duplicate_inventory(self):
        original = HERE.parent / "tables/ngii"
        metadata = ngii.csv_rows(original / "metadata/ard_metadata_2025.csv", ngii.META_REQUIRED)
        row = next(r for r in metadata if r["event_id"] == "E6" and r["acquisition_date"] == "20250730")
        manifest = next(r for r in ngii.csv_rows(original / "asset_manifest.csv", ngii.REQUIRED["asset_manifest.csv"]) if r["catalog_id"] == row["catalog_id"])
        (self.tables / "metadata/ard_metadata_2024.csv").unlink()
        (self.tables / "metadata/ard_aux_xml_2024.tar.gz").unlink()
        ngii.write_csv(self.tables / "metadata/ard_metadata_2025.csv", [row])
        ngii.write_csv(self.tables / "asset_manifest.csv", [manifest])
        with tarfile.open(original / "metadata/ard_aux_xml_2025.tar.gz") as archive:
            member = next(m for m in archive.getmembers() if Path(m.name).name == row["aux_xml"])
            data = archive.extractfile(member).read()
        with tarfile.open(self.tables / "metadata/ard_aux_xml_2025.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        result = ngii.build(self.repo, self.out)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["event_link_count"], 2)
        links = ngii.csv_rows(self.out / "image_event_links.csv", {"event_id"})
        self.assertEqual({(r["event_id"], r["phase"]) for r in links}, {("E6", "pre"), ("E4", "post")})


if __name__ == "__main__":
    unittest.main()
