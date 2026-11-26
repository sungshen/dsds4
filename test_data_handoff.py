"""Offline safety and integrity checks for the handoff sharing tool only."""
from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote, urlencode, urlsplit


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("handoff_share_tool", ROOT / "scripts/create_handoff_links.py")
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
ACCOUNT = "a" * 32
ENDPOINT = f"https://{ACCOUNT}.r2.cloudflarestorage.com"
KEY = "viirs-daily/gold/2024/01/01/dong-observations.parquet"


class FakeR2:
    """In-memory R2 stand-in; no credentials, sockets or production objects."""

    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url=ENDPOINT)
        self.sign_calls = []
        self.puts = []
        self.objects = {KEY: {"body": b"existing observation", "Metadata": {}}}
        self.url_override = None

    def generate_presigned_url(self, operation, *, Params, ExpiresIn, HttpMethod):
        self.sign_calls.append((operation, Params, ExpiresIn, HttpMethod))
        query = urlencode({"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                           "X-Amz-Credential": "OFFLINE/20261126/auto/s3/aws4_request",
                           "X-Amz-Date": "20261126T000000Z", "X-Amz-Expires": ExpiresIn,
                           "X-Amz-SignedHeaders": "host", "X-Amz-Signature": "b" * 64})
        url = f'{ENDPOINT}/{Params["Bucket"]}/{quote(Params["Key"], safe="/")}?{query}'
        return self.url_override(url) if self.url_override else url

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") != "*":
            raise AssertionError("Unconditional upload is forbidden")
        if key in self.objects:
            raise RuntimeError("PreconditionFailed")
        payload = kwargs["Body"].read()
        assert kwargs["ContentLength"] == len(payload)
        assert kwargs["ContentMD5"] == base64.b64encode(hashlib.md5(payload).digest()).decode()
        assert kwargs["Metadata"]["sha256"] == hashlib.sha256(payload).hexdigest()
        self.puts.append({**kwargs, "Body": payload})
        self.objects[key] = {"body": payload, "Metadata": kwargs["Metadata"].copy()}

    def head_object(self, *, Bucket, Key):
        obj = self.objects[Key]
        return {"ContentLength": len(obj["body"]), "Metadata": obj["Metadata"].copy()}


class ShareSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.repo = self.temp_path / "checkout"
        self.repo.mkdir()
        self.report = self.repo / "reports/2026-11-26-data-handoff"
        self.private = self.repo / "data/platform/sharing"
        patcher = mock.patch.multiple(handoff, ROOT=self.repo, REPORT=self.report, PRIVATE=self.private)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, text="public fixture\n"):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def inventory(self, rows, fields=None):
        path = self.temp_path / "objects.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or ["object_key", "tier", "size_bytes"], lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def ready_report(self, inventory):
        """Generate a coherent report using the real offline common-report code."""
        checker = self.write("scripts/build_data_handoff.py", (ROOT / "scripts/build_data_handoff.py").read_text())
        self.write("reports/2026-11-26-data-handoff/viirs/objects.csv", inventory.read_text())
        self.write("reports/2026-11-26-data-handoff/viirs/days.csv", "date,status\n2024-01-01,CONSISTENT_METADATA_AND_QUALITY\n")
        self.write("reports/2026-11-26-data-handoff/viirs/scenes.csv", "date,swath_id\n2024-01-01,example\n")
        self.write("reports/2026-11-26-data-handoff/ngii/image_inventory.csv", "acquisition_date,raw_recorded_size_bytes\n2024-01-01,20\n")
        documents = {
            "gk2a/quality_summary.json": {"status": "pass", "errors": [], "inventory_counts": {
                "originals": 1, "selected_daily_observations": 1, "bronze_only_extras": 0, "dongs": 1,
                "silver_and_mask_files": 2, "existing_dong_observation_qc_rows_checked": 1, "original_bytes_checked": 20}},
            "ngii/validation_summary.json": {"error_count": 0, "image_count": 1, "event_link_count": 1,
                "band_record_count": 1, "mask_record_count": 1, "raw_local_available_count": 0, "warning_count": 1},
            "viirs/summary.json": {"issue_severity_counts": {}, "small_json_failure_count": 0,
                "object_tier_counts": {"gold": 1, "bronze": 0}, "object_tier_bytes": {"bronze": 0},
                "quality_totals": {"pixel_count": 3, "finite_temperature": 3, "flagged": 0, "valid_flag_zero_temperature": 3},
                "gold_rows_recorded_server": 1, "scene_records": 1, "no_paired_cmr_dates": [], "object_count": 1, "object_bytes": 20},
            "viirs/scene_footprints_summary.json": {"status": "complete", "exact_single_metadata_matches": 1,
                "scene_inventory_sha256": hashlib.sha256((self.report / "viirs/scenes.csv").read_bytes()).hexdigest()},
            "gold/gold_validation_summary.json": {"status": "PASS", "files_checked": 1,
                "input_inventory_sha256": hashlib.sha256((self.report / "viirs/objects.csv").read_bytes()).hexdigest(),
                "input_days_sha256": hashlib.sha256((self.report / "viirs/days.csv").read_bytes()).hexdigest(),
                "input_scenes_sha256": hashlib.sha256((self.report / "viirs/scenes.csv").read_bytes()).hexdigest(),
                "totals": {"pixel_count": 3, "finite_temperature_count": 3, "flagged_count": 0, "valid_count": 3},
                "row_count": 1, "total_gold_bytes": 20, "temperature_min_k": 250, "temperature_max_k": 250},
        }
        for name, value in documents.items():
            self.write("reports/2026-11-26-data-handoff/" + name, json.dumps(value))
        spec = importlib.util.spec_from_file_location("fixture_handoff_builder", checker)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with contextlib.redirect_stdout(io.StringIO()):
            builder.main([])

    def test_main_rejects_missing_failed_or_stale_reports_before_api_operations(self):
        inventory = self.inventory([{"object_key": KEY, "tier": "gold", "size_bytes": 20}])
        for case in ("missing", "failed", "stale_gold_binding", "wrong_totals", "stale_common_status", "stale_checksums"):
            with self.subTest(case=case):
                self.ready_report(inventory)
                gold_file = self.report / "gold/gold_validation_summary.json"
                gold = json.loads(gold_file.read_text())
                if case == "missing":
                    gold_file.unlink()
                elif case == "failed":
                    gold["status"] = "FAIL"
                    gold_file.write_text(json.dumps(gold))
                elif case == "stale_gold_binding":
                    gold["input_inventory_sha256"] = "0" * 64
                    gold_file.write_text(json.dumps(gold))
                elif case == "wrong_totals":
                    gold["totals"]["pixel_count"] = 4
                    gold_file.write_text(json.dumps(gold))
                elif case == "stale_common_status":
                    common_file = self.report / "summary.json"
                    common = json.loads(common_file.read_text())
                    common["metadata_integrity_status"] = "FAIL"
                    common_file.write_text(json.dumps(common))
                else:
                    (self.report / "SHA256SUMS.csv").write_text("stale checksum fixture\n")
                load_config, create_client = mock.Mock(), mock.Mock()
                with mock.patch.dict(sys.modules, {"dotenv": SimpleNamespace(dotenv_values=load_config),
                                                  "boto3": SimpleNamespace(client=create_client)}), \
                        mock.patch.object(handoff, "signed_get") as sign, \
                        mock.patch.object(handoff, "upload_share_file") as upload:
                    with self.assertRaisesRegex(ValueError, "missing, failed or stale"):
                        handoff.main(["--inventory", str(inventory), "--publish"])
                load_config.assert_not_called()
                create_client.assert_not_called()
                sign.assert_not_called()
                upload.assert_not_called()
                self.assertFalse(self.private.exists())

    def test_main_rejects_alternate_inventory_unbound_to_verified_report(self):
        inventory = self.inventory([{"object_key": KEY, "tier": "gold", "size_bytes": 20}])
        self.ready_report(inventory)
        inventory.write_text(inventory.read_text().replace("size_bytes", "size"))
        with mock.patch.object(handoff, "signed_get") as sign, mock.patch.object(handoff, "upload_share_file") as upload:
            with self.assertRaisesRegex(ValueError, "must match"):
                handoff.main(["--inventory", str(inventory), "--publish"])
        sign.assert_not_called()
        upload.assert_not_called()

    def test_package_contains_only_named_public_inputs_and_verified_checksums(self):
        self.write("README.md", "public readme\n")
        self.write("docs/DATA_HANDOFF.md", "public instructions\n")
        self.write("reports/2026-11-26-data-handoff/gk2a/cleaned_observations.csv", "time,dn\n2024-01-01,4500\n")
        private_names = [".env", "platform/.env", "data/platform/sharing/receipt.json",
                         "data/platform/server-core.tar.gz", "docs/private-backup.md",
                         "tables/ngii/private_backup.json",
                         "reports/2026-11-26-data-handoff/backup/credentials.json",
                         "reports/2026-11-26-data-handoff/viirs/downloads.csv"]
        for name in private_names:
            self.write(name, "PRIVATE_FIXTURE_DO_NOT_ARCHIVE")
        dest = self.temp_path / "metadata.zip"
        self.assertEqual(handoff.make_package(dest, "c" * 40), 3)
        with zipfile.ZipFile(dest) as archive:
            self.assertEqual(set(archive.namelist()), {
                "README.md", "docs/DATA_HANDOFF.md",
                "reports/2026-11-26-data-handoff/gk2a/cleaned_observations.csv",
                "START_HERE.txt", "SHA256SUMS.json"})
            for entry in json.loads(archive.read("SHA256SUMS.json")):
                payload = archive.read(entry["path"])
                self.assertEqual(len(payload), entry["size_bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["sha256"])
            self.assertNotIn(b"PRIVATE_FIXTURE", b"".join(archive.read(name) for name in archive.namelist()))
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    def test_bearer_urls_and_private_keys_in_allowed_files_are_rejected(self):
        path = self.write("README.md")
        for content in [f"https://{ACCOUNT}.r2.cloudflarestorage.com/bucket/file?X-Amz-Signature=secret",
                        "https://api.example/data?date=2024&authKey=PRIVATE",
                        "-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE\n"]:
            with self.subTest(content=content.split("?")[0]):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    handoff.make_package(self.temp_path / "blocked.zip", "c" * 40)

    def test_symlinked_file_and_symlinked_parent_are_rejected(self):
        external = self.temp_path / "private.md"
        external.write_text("PRIVATE")
        link = self.repo / "README.md"
        link.symlink_to(external)
        with self.assertRaises(ValueError):
            handoff.package_paths()
        link.unlink()
        external_dir = self.temp_path / "external-docs"
        external_dir.mkdir()
        (external_dir / "DATA_HANDOFF.md").write_text("PRIVATE")
        (self.repo / "docs").symlink_to(external_dir, target_is_directory=True)
        with self.assertRaises(ValueError):
            handoff.package_paths()

    def test_xml_archive_cannot_smuggle_private_or_traversal_members(self):
        path = self.repo / "tables/ngii/metadata/ard_aux_xml_2024.tar.gz"
        path.parent.mkdir(parents=True)
        for name in ("E1/L3A_example_Aux.xml", "../.env", "E1/private-backup.json"):
            with tarfile.open(path, "w:gz") as archive:
                info = tarfile.TarInfo(name)
                info.size = len(b"<metadata/>")
                archive.addfile(info, io.BytesIO(b"<metadata/>"))
            with self.subTest(name=name):
                if name.endswith("_Aux.xml"):
                    self.assertEqual(handoff.package_paths(), [path])
                else:
                    with self.assertRaises(ValueError):
                        handoff.package_paths()

    def test_inventory_supports_nested_prefix_and_derives_exact_tier(self):
        key = "project/viirs/daily/gold/2024/01/01/result.parquet"
        path = self.inventory([{"object_key": key, "size_bytes": 17}], ["object_key", "size_bytes"])
        rows = handoff.read_objects(path, "project/viirs/daily")
        self.assertEqual(rows[0]["tier"], "gold")
        self.assertEqual(rows[0]["size_bytes"], 17)
        self.assertEqual(rows[0]["object_key"], key)

    def test_inventory_rejects_ambiguous_paths_outside_prefix_and_tier_mismatch(self):
        invalid = ["viirs-daily/gold/../bronze/file", "viirs-daily/./gold/file",
                   "viirs-daily//gold/file", "/viirs-daily/gold/file",
                   "viirs-daily/gold\\file", "viirs-daily/gold/file\n",
                   "viirs-daily/gold/%2e%2e/bronze/file", "viirs-daily/gold/%2Ffile",
                   "viirs-daily-other/gold/file", "other/gold/file", "viirs-daily/private/file"]
        for key in invalid:
            with self.subTest(key=key):
                path = self.inventory([{"object_key": key, "tier": "gold", "size_bytes": 1}])
                with self.assertRaises(ValueError):
                    handoff.read_objects(path, "viirs-daily")
        path = self.inventory([{"object_key": KEY, "tier": "bronze", "size_bytes": 1}])
        with self.assertRaises(ValueError):
            handoff.read_objects(path, "viirs-daily")

    def test_inventory_rejects_duplicate_keys_empty_and_unknown_sizes(self):
        row = {"object_key": KEY, "tier": "gold", "size_bytes": 1}
        for rows in ([row, row], [], [{**row, "size_bytes": 0}], [{**row, "size_bytes": ""}], [{**row, "size_bytes": -1}]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    handoff.read_objects(self.inventory(rows), "viirs-daily")

    def test_signed_links_are_get_only_with_exact_lifetime_and_expiry(self):
        client = FakeR2()
        for seconds in (1, 3600, 604800):
            url, expiry = handoff.signed_get(client, "bucket", KEY, seconds)
            self.assertEqual(expiry, datetime(2026, 11, 26, tzinfo=timezone.utc) + timedelta(seconds=seconds))
            self.assertEqual(urlsplit(url).hostname, f"{ACCOUNT}.r2.cloudflarestorage.com")
            self.assertEqual(client.sign_calls[-1], ("get_object", {"Bucket": "bucket", "Key": KEY}, seconds, "GET"))
        before = len(client.sign_calls)
        for seconds in (0, -1, 604801, True, 1.5):
            with self.assertRaises(ValueError):
                handoff.signed_get(client, "bucket", KEY, seconds)
        self.assertEqual(len(client.sign_calls), before)

    def test_signed_links_reject_wrong_host_scheme_path_or_expiry(self):
        changes = [lambda u: u.replace("https://", "http://"),
                   lambda u: u.replace(ACCOUNT, "d" * 32),
                   lambda u: u.replace(".r2.cloudflarestorage.com", ".r2.cloudflarestorage.com.evil.example"),
                   lambda u: u.replace("/bucket/", "/different-bucket/"),
                   lambda u: u.replace("X-Amz-Expires=3600", "X-Amz-Expires=604801"),
                   lambda u: u + "&X-Amz-Expires=3600", lambda u: u + "#fragment"]
        for change in changes:
            with self.subTest(change=change):
                client = FakeR2()
                client.url_override = change
                with self.assertRaises(ValueError):
                    handoff.signed_get(client, "bucket", KEY, 3600)
        client = FakeR2()
        client.meta.endpoint_url = "https://example.com"
        with self.assertRaises(ValueError):
            handoff.signed_get(client, "bucket", KEY, 3600)
        self.assertEqual(client.sign_calls, [])

    def test_upload_cannot_modify_observations_or_overwrite_existing_share(self):
        client = FakeR2()
        path = self.write("package.zip", "new package")
        original = client.objects[KEY]["body"]
        for key in (KEY, "handoff/../viirs-daily/gold/file", "handoff/pkg/../../gold/file",
                    "handoff//package.zip", "handoff/pkg/%2e%2e/observations", "handoff/file.zip"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    handoff.upload_share_file(client, "bucket", key, path, "application/zip")
        self.assertEqual(client.puts, [])
        handoff.upload_share_file(client, "bucket", "handoff/run/package.zip", path, "application/zip")
        self.assertEqual(client.objects[KEY]["body"], original)
        with self.assertRaises(RuntimeError):
            handoff.upload_share_file(client, "bucket", "handoff/run/package.zip", path, "application/zip")
        self.assertEqual(len(client.puts), 1)

    def test_upload_rejects_mismatched_head_metadata(self):
        client = FakeR2()
        path = self.write("package.zip", "new package")
        with mock.patch.object(client, "head_object", return_value={"ContentLength": 11, "Metadata": {"sha256": "0" * 64}}):
            with self.assertRaises(ValueError):
                handoff.upload_share_file(client, "bucket", "handoff/run/package.zip", path, "application/zip")

    @unittest.skipUnless(importlib.util.find_spec("boto3") and importlib.util.find_spec("dotenv"), "optional CLI dependencies unavailable")
    def test_default_preparation_does_not_upload_any_object(self):
        self.write("README.md", "public handoff")
        path = self.inventory([{"object_key": KEY, "tier": "gold", "size_bytes": 20}])
        self.ready_report(path)
        client = FakeR2()
        config = {"R2_ACCOUNT_ID": ACCOUNT, "R2_BUCKET": "bucket", "R2_ACCESS_KEY_ID": "offline-id",
                  "R2_SECRET_ACCESS_KEY": "offline-secret", "R2_PREFIX": "viirs-daily"}
        with mock.patch("dotenv.dotenv_values", return_value=config), mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("boto3.client", return_value=client), \
                mock.patch.object(handoff.subprocess, "check_output", return_value="c" * 40 + "\n"), \
                contextlib.redirect_stdout(io.StringIO()):
            receipt_path = handoff.main(["--inventory", str(path)])
        receipt = json.loads(receipt_path.read_text())
        self.assertFalse(receipt["published"])
        self.assertEqual(receipt["index_url"], "")
        self.assertEqual(client.puts, [])
        self.assertEqual(set(client.objects), {KEY})
        self.assertEqual(client.objects[KEY]["body"], b"existing observation")

    @unittest.skipUnless(importlib.util.find_spec("boto3") and importlib.util.find_spec("dotenv"), "optional CLI dependencies unavailable")
    def test_publish_flow_uploads_only_three_share_files_and_keeps_links_private(self):
        self.write("README.md", "public handoff")
        self.write("docs/DATA_HANDOFF.md", "public instructions")
        path = self.inventory([{"object_key": KEY, "tier": "gold", "size_bytes": 20}])
        self.ready_report(path)
        client = FakeR2()
        config = {"R2_ACCOUNT_ID": ACCOUNT, "R2_BUCKET": "bucket", "R2_ACCESS_KEY_ID": "offline-id",
                  "R2_SECRET_ACCESS_KEY": "offline-secret", "R2_PREFIX": "viirs-daily"}
        output = io.StringIO()
        with mock.patch("dotenv.dotenv_values", return_value=config), mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("boto3.client", return_value=client), \
                mock.patch.object(handoff.subprocess, "check_output", return_value="c" * 40 + "\n"), \
                contextlib.redirect_stdout(output):
            receipt_path = handoff.main(["--inventory", str(path), "--expires-seconds", "3600", "--publish"])
        receipt = json.loads(receipt_path.read_text())
        self.assertTrue(receipt["published"])
        self.assertFalse(receipt["observation_objects_changed"])
        self.assertEqual(len(client.puts), 3)
        self.assertEqual({Path(row["Key"]).name for row in client.puts}, {"dsds-data-handoff.zip", "viirs-downloads.csv", "index.html"})
        self.assertTrue(all(row["Key"].startswith("handoff/") for row in client.puts))
        self.assertEqual(client.objects[KEY]["body"], b"existing observation")
        self.assertTrue(all(call[0] == "get_object" and call[3] == "GET" and call[2] == 3600 for call in client.sign_calls))
        self.assertNotIn("X-Amz-", output.getvalue())
        self.assertNotIn("offline-secret", output.getvalue())
        for file in receipt_path.parent.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt_path.parent.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(importlib.util.find_spec("boto3"), "optional signer dependency unavailable")
    def test_real_sdk_presigning_is_offline_and_passes_r2_validation(self):
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", endpoint_url=ENDPOINT, region_name="auto",
                              aws_access_key_id="OFFLINEEXAMPLE", aws_secret_access_key="offline-example-secret",
                              config=Config(signature_version="s3v4"))
        with mock.patch.object(client, "_make_api_call", side_effect=AssertionError("Network/API calls forbidden")):
            url, expiry = handoff.signed_get(client, "bucket", KEY, 604800)
        self.assertIn("X-Amz-Signature=", url)
        self.assertGreater(expiry, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
