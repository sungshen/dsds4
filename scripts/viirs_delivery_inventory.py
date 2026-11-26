#!/usr/bin/env python3
"""Read-only, cache-aware VIIRS delivery inventory; never downloads full Parquet.

Uses LIST for all objects, GET for small publication/quality JSON, and HEAD for
three deterministic daily samples. No NASA queries, processing, or R2 writes.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from viirs_r2_audit import decode_digest, parse_key  # Existing digest/key contract.

KST = timezone(timedelta(hours=9))
PRODUCTS = ("VNP02IMG", "VNP03IMG")
COUNTS = ("pixel_count", "finite_temperature", "flagged", "valid_flag_zero_temperature")
SOURCE_NAME = re.compile(r"(VNP0[23]IMG)\.(A\d{7}\.\d{4})\.(\d{3})\.(\d{13})\.nc")
DAILY_NAMES = {"silver": "all-observations.parquet", "quality": "all-observations.json",
               "gold": "dong-observations.parquet"}
SERVER_COLUMNS = ("date", "status", "scene_count", "silver_pixels", "gold_rows",
                  "obsolete_deleted", "updated_at_utc", "pipeline_version")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "reports/2026-11-26-data-handoff/viirs")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/platform/viirs-delivery-cache")
    parser.add_argument("--server-archive", type=Path,
                        default=ROOT / "data/platform/hetzner-retirement/20261126/server-core.tar.gz")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--offline", action="store_true", help="Rebuild reports from the saved LIST snapshot and JSON cache")
    parser.add_argument("--refresh", action="store_true", help="Refetch small JSON even when its LIST ETag is unchanged")
    return parser.parse_args()


def sha(value):
    try:
        return decode_digest(value, "sha256").hex()
    except (ValueError, TypeError):
        return None


def error_code(exc):
    return str(exc.response.get("Error", {}).get("Code", "ClientError")) if isinstance(exc, ClientError) else type(exc).__name__


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def client_config(workers):
    settings = {}
    for layer in (dotenv_values(ROOT / ".env"), dotenv_values(ROOT / "platform/.env"), os.environ):
        settings.update({k: v for k, v in layer.items() if v is not None and str(v).strip()})
    required = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not all(settings.get(k) for k in required):
        raise ValueError("Missing R2 settings; use the existing private dotenv files")
    account = settings["R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise ValueError("Invalid R2 account setting")
    client = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                          region_name="auto", aws_access_key_id=settings["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=settings["R2_SECRET_ACCESS_KEY"],
                          config=Config(connect_timeout=10, read_timeout=40,
                                        retries={"mode": "standard", "max_attempts": 3},
                                        max_pool_connections=workers + 3, signature_version="s3v4"))
    return client, settings["R2_BUCKET"], settings.get("R2_PREFIX", "viirs-daily").strip("/")


def list_snapshot(client, bucket, prefix):
    objects = []
    pages = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        pages += 1
        for item in page.get("Contents", []):
            if not item["Key"].endswith("/"):
                objects.append({"key": item["Key"], "size_bytes": int(item["Size"]),
                                "etag": item.get("ETag", "").strip('"'),
                                "last_modified_utc": item["LastModified"].isoformat()})
    objects.sort(key=lambda row: row["key"])
    return {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "prefix": prefix,
            "list_pages": pages, "objects": objects,
            "snapshot_sha256": hashlib.sha256(json.dumps(objects, sort_keys=True).encode()).hexdigest()}


def archived_server_report(archive, out):
    """Read only known, safe report members; never extract the private archive."""
    csv_path = out / "server_daily_status.csv"
    evidence_path = out / "server_report_evidence.json"
    if not archive.is_file():
        if not csv_path.is_file() or not evidence_path.is_file():
            raise ValueError("A safe server report snapshot or the private retirement archive is required")
        return read_csv(csv_path), json.loads(evidence_path.read_text())
    daily = {}
    csv_rows = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if re.fullmatch(r"(?:root/viirs/|viirs/)?full_rebuild_report/days/\d{4}-\d{2}-\d{2}\.json", member.name):
                payload = json.load(tar.extractfile(member))
                daily[payload["date"]] = {key: payload.get(key, "") for key in SERVER_COLUMNS}
            elif member.name in ("root/viirs/full_rebuild_report/daily_results.csv", "viirs/full_rebuild_report/daily_results.csv", "full_rebuild_report/daily_results.csv"):
                csv_rows = list(csv.DictReader(io.StringIO(tar.extractfile(member).read().decode("utf-8-sig"))))
    if len(daily) != 731:
        raise ValueError("Expected 731 unique archived daily server reports")
    rows = [daily[key] for key in sorted(daily)]
    duplicate_dates = sorted(key for key, count in Counter(row["date"] for row in csv_rows).items() if count > 1)
    archive_hash = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            archive_hash.update(chunk)
    evidence = {
        "archive_reference": "Private retired-server backup; identified by SHA-256 only",
        "archive_sha256": archive_hash.hexdigest(),
        "safe_source_member_pattern": "full_rebuild_report/days/YYYY-MM-DD.json",
        "authoritative_unique_days": len(rows),
        "daily_results_csv_rows": len(csv_rows), "daily_results_csv_duplicate_dates": duplicate_dates,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "no_paired_cmr_dates": [row["date"] for row in rows if row["status"] == "NO_PAIRED_CMR"],
        "deduplicated_totals": {key: sum(int(row[key] or 0) for row in rows)
                                 for key in ("scene_count", "silver_pixels", "gold_rows")},
        "latest_server_report_utc": max(str(row["updated_at_utc"]) for row in rows),
        "interpretation": "Unique per-day JSON is authoritative; appended CSV repeats some days. NO_PAIRED_CMR is conditional on the configured search, not proof of no satellite observation.",
    }
    write_csv(csv_path, rows, SERVER_COLUMNS)
    write_json(evidence_path, evidence)
    return rows, evidence


def fetch_small(client, bucket, item, cache, offline=False, refresh=False):
    key = item["key"]
    path = cache / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    if path.is_file() and not refresh:
        saved = json.loads(path.read_text())
        if (saved.get("etag") == item["etag"] and saved.get("key") == key
                and saved.get("last_modified_utc") == item["last_modified_utc"]
                and saved.get("size_bytes") == item["size_bytes"]):
            raw = base64.b64decode(saved["body_base64"], validate=True)
            if len(raw) == item["size_bytes"]:
                return key, {"payload": json.loads(raw), "body_sha256": hashlib.sha256(raw).hexdigest(),
                             "metadata": saved["metadata"], "retrieval": "etag_matched_cache"}
    if offline:
        raise ValueError("Missing matching small-JSON cache")
    if item["size_bytes"] > 2 * 1024 * 1024:
        raise ValueError("Refusing oversized JSON")
    response = client.get_object(Bucket=bucket, Key=key, IfMatch='"' + item["etag"] + '"')
    with response["Body"] as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) != item["size_bytes"]:
        raise ValueError("JSON length differs from LIST snapshot")
    # R2 LIST retains milliseconds; GET/HEAD Last-Modified is HTTP-second precision.
    listed_modified = datetime.fromisoformat(item["last_modified_utc"])
    if response["LastModified"].replace(microsecond=0) != listed_modified.replace(microsecond=0):
        raise ValueError("JSON metadata changed since LIST snapshot")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("JSON is not an object")
    metadata = {key: response.get("Metadata", {}).get(key, "") for key in ("sha256", "pipeline-version", "input-fingerprint")}
    saved = {"key": key, "etag": item["etag"], "last_modified_utc": item["last_modified_utc"],
             "size_bytes": item["size_bytes"], "body_base64": base64.b64encode(raw).decode(), "metadata": metadata}
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return key, {"payload": payload, "body_sha256": hashlib.sha256(raw).hexdigest(),
                 "metadata": metadata, "retrieval": "get_if_match"}


def audit(snapshot, fetched, failures, server_rows, head_checks):
    prefix = snapshot["prefix"]
    objects = {row["key"]: dict(row) for row in snapshot["objects"]}
    server = {row["date"]: row for row in server_rows}
    issues, scenes, assets, days = [], [], [], []
    source_hashes = defaultdict(set)
    source_references = Counter()
    bronze_logical = defaultdict(list)

    def issue(day, code, detail, key="", severity="error"):
        issues.append({"date_kst": str(day), "severity": severity, "code": code, "object_key": key, "detail": detail})

    for key, item in objects.items():
        parsed = parse_key(prefix, key)
        if parsed:
            tier, boundary, day, filename = parsed
            item.update(tier=tier, boundary_version=boundary or "", date_kst=str(day), filename=filename)
            if tier == "bronze":
                match = SOURCE_NAME.fullmatch(filename)
                if match:
                    bronze_logical[(str(day), match[1], match[2])].append(key)
                else:
                    issue(day, "UNRECOGNIZED_BRONZE_NAME", "Filename does not match the supported VNP Collection naming contract", key, "warning")
            elif tier != "publication" and filename != DAILY_NAMES.get(tier):
                issue(day, "NONCANONICAL_DERIVED_OBJECT", "Legacy or additional derived object remains", key, "warning")
        else:
            item.update(tier="other", boundary_version="", date_kst="", filename=key.rsplit("/", 1)[-1])
            issue("", "UNCLASSIFIED_OBJECT", "Object is included in inventory but outside the documented daily layout", key, "warning")
    for key, reason in failures.items():
        issue(objects[key].get("date_kst", ""), "SMALL_JSON_READ_FAILED", reason, key)
    for logical, keys in sorted(bronze_logical.items()):
        if len(keys) > 1:
            issue(logical[0], "MULTIPLE_SOURCE_VERSIONS", f"{len(keys)} stored files for product={logical[1]} swath={logical[2]}; distinct versions are retained, not counted as new observations", severity="warning")

    start, stop = date(2024, 1, 1), date(2026, 1, 1)
    for offset in range((stop - start).days):
        day = start + timedelta(days=offset)
        ds = str(day)
        version = f"sgis-{day.year}-2q"
        base = f"{version}/{day:%Y/%m/%d}"
        keys = {tier: f"{prefix}/{tier}/{base}/{name}" for tier, name in DAILY_NAMES.items()}
        marker_key = f"{prefix}/quality/{base}/completion.json"
        server_row = server.get(ds, {})
        row = {"date_kst": ds, "boundary_version": version, "server_status": server_row.get("status", ""),
               "publication_check_level": "LIST+manifest sizes; GET quality SHA+metadata; sample HEAD separately",
               "cloud_qc_status": "unverified", "silver_key": keys["silver"], "quality_key": keys["quality"],
               "gold_key": keys["gold"], "completion_key": marker_key,
               "silver_rows_recorded_server": server_row.get("silver_pixels", ""),
               "gold_rows_recorded_server": server_row.get("gold_rows", ""),
               "gold_row_count_basis": "archived unique daily server report; not newly read Parquet"}
        before_errors = sum(item["severity"] == "error" for item in issues)
        if marker_key not in objects:
            any_products = any(key in objects for key in keys.values())
            if server_row.get("status") == "NO_PAIRED_CMR" and not any_products:
                row.update(status="NO_PAIRED_CMR", scene_count=0, pixel_count="", finite_temperature="", flagged="", valid_flag_zero_temperature="")
                issue(ds, "NO_PAIRED_CMR", "Archived search found no matching VNP02IMG/VNP03IMG pair; no zero-valued observation is invented", severity="expected_gap")
            else:
                row["status"] = "MISSING_OR_UNCOMMITTED"
                issue(ds, "MISSING_COMPLETION", "Missing daily completion marker")
            days.append(row)
            continue
        if marker_key not in fetched or keys["quality"] not in fetched:
            row["status"] = "JSON_READ_FAILED"
            days.append(row)
            continue
        publication = fetched[marker_key]
        quality_data = fetched[keys["quality"]]
        manifest, quality = publication["payload"], quality_data["payload"]
        fingerprint, pipeline = manifest.get("input_fingerprint", ""), manifest.get("pipeline_version", "")
        row.update(input_fingerprint=fingerprint, pipeline_version=pipeline,
                   completion_last_modified_utc=objects[marker_key]["last_modified_utc"],
                   quality_status=quality.get("status", ""), processing_code_sha256=quality.get("processing_code_sha256", ""),
                   boundary_sha256=quality.get("boundary_sha256", ""))
        if manifest.get("manifest_version") != 1 or not re.fullmatch(r"[a-f0-9]{64}", fingerprint) or not pipeline:
            issue(ds, "INVALID_MANIFEST", "Unsupported marker version or missing processing fingerprint/version", marker_key)
        for field, expected in (("date_kst", ds), ("boundary_version", version)):
            if manifest.get(field) != expected or quality.get(field) != expected:
                issue(ds, "DATE_OR_BOUNDARY_MISMATCH", field, marker_key)
        for field in ("pipeline_version", "input_fingerprint"):
            if quality.get(field) != manifest.get(field):
                issue(ds, "QUALITY_LINEAGE_MISMATCH", field, keys["quality"])
        for tier, key in keys.items():
            declaration = manifest.get("objects", {}).get(tier, {})
            if declaration.get("key") != key or not sha(declaration.get("sha256")):
                issue(ds, "MANIFEST_OBJECT_DECLARATION", f"Invalid {tier} path or SHA-256", marker_key)
            if key not in objects:
                issue(ds, "MISSING_DERIVED_OBJECT", tier, key)
                continue
            item = objects[key]
            item["declared_sha256_hex"] = sha(declaration.get("sha256")) or ""
            item["checksum_basis"] = "completion manifest declaration; full content not rehashed"
            if item["size_bytes"] != declaration.get("size_bytes"):
                issue(ds, "LIST_MANIFEST_SIZE_MISMATCH", tier, key)
            if item["last_modified_utc"] > objects[marker_key]["last_modified_utc"]:
                issue(ds, "OBJECT_NEWER_THAN_COMPLETION", tier, key)
        for key, small in ((marker_key, publication), (keys["quality"], quality_data)):
            meta = small["metadata"]
            if sha(meta.get("sha256")) != small["body_sha256"]:
                issue(ds, "JSON_BODY_SHA_MISMATCH", "GET content differs from stored SHA-256 metadata", key)
            if meta.get("input-fingerprint") != fingerprint or meta.get("pipeline-version") != pipeline:
                issue(ds, "JSON_OBJECT_METADATA_LINEAGE", "GET object metadata differs from completion", key)
            objects[key]["observed_body_sha256_hex"] = small["body_sha256"]
            objects[key]["checksum_basis"] = "GET JSON body SHA-256 verified"
        if quality_data["body_sha256"] != sha(manifest.get("objects", {}).get("quality", {}).get("sha256")):
            issue(ds, "QUALITY_BODY_MANIFEST_SHA_MISMATCH", "Quality bytes differ from publication declaration", keys["quality"])
        per_scene = quality.get("per_scene", {})
        row["scene_count"] = len(per_scene)
        if quality.get("source_scene_count") != len(per_scene):
            issue(ds, "SCENE_COUNT_MISMATCH", "Quality source_scene_count differs from unique per_scene entries")
        valid_daily_count_types = True
        for name in COUNTS:
            row[name] = quality.get(name, "")
            values = [item.get(name, "") for item in per_scene.values()]
            valid_types = type(row[name]) is int and row[name] >= 0 and all(type(value) is int and value >= 0 for value in values)
            total = sum(values) if valid_types else None
            if not valid_types or row[name] != total:
                issue(ds, "QUALITY_COUNT_MISMATCH", name)
            valid_daily_count_types = valid_daily_count_types and valid_types
        if str(server_row.get("silver_pixels", "")) != str(row["pixel_count"]) or str(server_row.get("scene_count", "")) != str(len(per_scene)):
            issue(ds, "SERVER_QUALITY_COUNT_MISMATCH", "Current quality counts differ from archived unique daily report")
        if valid_daily_count_types:
            row["nonfinite_temperature_count"] = row["pixel_count"] - row["finite_temperature"]
            row["excluded_from_gold_temperature_count"] = row["pixel_count"] - row["valid_flag_zero_temperature"]
            row["valid_fraction"] = row["valid_flag_zero_temperature"] / row["pixel_count"] if row["pixel_count"] else ""
            row["finite_fraction"] = row["finite_temperature"] / row["pixel_count"] if row["pixel_count"] else ""
        if quality.get("cloud_qc_status") != "unverified":
            issue(ds, "UNEXPECTED_CLOUD_QC_CLAIM", "This handoff supports only unverified cloud QC")
        day_units = True
        for swath, stat in sorted(per_scene.items()):
            metadata = stat.get("source_metadata", {})
            products = metadata.get("products", {})
            try:
                named_time = datetime.strptime(swath, "A%Y%j.%H%M").replace(tzinfo=timezone.utc)
            except ValueError:
                issue(ds, "INVALID_SWATH_ID", swath)
                continue
            attrs = products.get("VNP02IMG", {}).get("attributes", {})
            start_time = attrs.get("time_coverage_start") or named_time.isoformat()
            end_time = attrs.get("time_coverage_end", "")
            actual_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            if actual_time.tzinfo is None or abs((actual_time - named_time).total_seconds()) >= 60:
                issue(ds, "SOURCE_TIME_MISMATCH", swath)
            if str(named_time.astimezone(KST).date()) != ds:
                issue(ds, "SOURCE_KST_DATE_MISMATCH", swath)
            pixels, finite, flagged, valid = (stat.get(name, -1) for name in COUNTS)
            if not all(type(value) is int for value in (pixels, finite, flagged, valid)):
                issue(ds, "INVALID_SCENE_QA_COUNTS", swath + ": noninteger counts")
                continue
            if not (max(0, finite - flagged) <= valid <= finite <= pixels and 0 <= flagged <= pixels and valid <= pixels - flagged):
                issue(ds, "INVALID_SCENE_QA_COUNTS", swath)
            lut_unit = products.get("VNP02IMG", {}).get("variables", {}).get("observation_data/I05_brightness_temperature_lut", {}).get("units", "")
            unit_ok = lut_unit.strip().lower() in ("k", "kelvin", "kelvins")
            day_units = day_units and unit_ok
            if not unit_ok:
                issue(ds, "TEMPERATURE_UNIT_UNVERIFIED", swath)
            scene = {"date_kst": ds, "swath_id": swath, "platform": attrs.get("platform", ""),
                     "instrument": attrs.get("instrument", ""), "time_start_utc": start_time,
                     "time_end_utc": end_time, "time_start_kst": actual_time.astimezone(KST).isoformat(),
                     "time_end_kst": datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(KST).isoformat() if end_time else "",
                     "time_basis": "source time_coverage_start" if attrs.get("time_coverage_start") else "granule ID minute",
                     **{name: stat.get(name, "") for name in COUNTS},
                     "nonfinite_temperature_count": pixels - finite, "excluded_temperature_count": pixels - valid,
                     "valid_fraction": valid / pixels if pixels else "", "temperature_units": "K",
                     "source_lut_units": lut_unit, "unit_check": "declared_Kelvin" if unit_ok else "unverified",
                     "source_grid": metadata.get("native_grid", ""), "resampling": metadata.get("resampling", ""),
                     "query_bbox_wsen": "124,32,132,39", "query_bbox_basis": "configured CMR search; not measured footprint",
                     "measured_pixel_bbox_wsen": "", "measured_coverage_fraction": "",
                     "cloud_qc_status": "unverified", "boundary_version": version,
                     "pipeline_version": pipeline, "input_fingerprint": fingerprint,
                     "boundary_sha256": quality.get("boundary_sha256", ""),
                     "processing_code_sha256": quality.get("processing_code_sha256", "")}
            for product in PRODUCTS:
                source = products.get(product, {})
                filename = source.get("filename", "")
                match = SOURCE_NAME.fullmatch(filename)
                source_key = f"{prefix}/bronze/{day:%Y/%m/%d}/{filename}"
                source_sha = sha(stat.get("source_sha256", {}).get(product))
                if not match or match[1] != product or match[2] != swath:
                    issue(ds, "SOURCE_PRODUCT_ID_MISMATCH", product + " " + swath)
                elif match[3] != "002":
                    issue(ds, "SOURCE_COLLECTION_VERSION_MISMATCH", product + " " + swath)
                if source_key not in objects:
                    issue(ds, "MISSING_REFERENCED_BRONZE", product + " " + swath, source_key)
                if not source_sha:
                    issue(ds, "SOURCE_SHA_MISSING", product + " " + swath, source_key)
                elif source_key in objects:
                    source_hashes[source_key].add(source_sha)
                source_references[source_key] += 1
                source_attrs = source.get("attributes", {})
                band_variables = source.get("variables", {})
                variable_name = "observation_data/I05" if product == "VNP02IMG" else "geolocation_data/longitude"
                variable = band_variables.get(variable_name, {})
                assets.append({"date_kst": ds, "swath_id": swath, "product_id": product,
                               "filename": filename, "object_key": source_key,
                               "size_bytes_list": objects.get(source_key, {}).get("size_bytes", ""),
                               "sha256_hex_declared_quality": source_sha or "",
                               "checksum_check_level": "source hash recorded by processing; Bronze bytes not rehashed in this audit",
                               "collection_version": match[3] if match else "", "production_stamp_utc": match[4] if match else "",
                               "platform": source_attrs.get("platform", ""), "instrument": source_attrs.get("instrument", ""),
                               "processing_level": source_attrs.get("processing_level", ""),
                               "time_start_utc": source_attrs.get("time_coverage_start", ""),
                               "time_end_utc": source_attrs.get("time_coverage_end", ""),
                               "source_variable": variable_name, "source_shape": "x".join(map(str, variable.get("shape", []))),
                               "source_variable_units": variable.get("units", ""),
                               "provider_spatial_resolution": source_attrs.get("spatialResolution", ""),
                               "query_bbox_wsen": "124,32,132,39", "measured_footprint_bbox_wsen": ""})
                scene[product + "_filename"] = filename
                scene[product + "_sha256_hex"] = source_sha or ""
            scenes.append(scene)
        row["unit_check"] = "all_source_LUTs_declared_Kelvin" if day_units else "unverified"
        new_errors = sum(item["severity"] == "error" for item in issues) - before_errors
        row["status"] = "CONSISTENT_METADATA_AND_QUALITY" if new_errors == 0 else "REVIEW"
        days.append(row)

    for key, hashes in source_hashes.items():
        if len(hashes) != 1:
            issue(objects[key].get("date_kst", ""), "SOURCE_CHECKSUM_CONFLICT", "Same Bronze key has differing declared hashes", key)
        objects[key]["declared_sha256_hex"] = ";".join(sorted(hashes))
        objects[key]["checksum_basis"] = "quality source_sha256 declaration; full Bronze content not rehashed"
    for key, item in objects.items():
        if item["tier"] == "bronze" and not source_references[key]:
            issue(item.get("date_kst", ""), "UNREFERENCED_BRONZE", "Stored raw asset not referenced by current daily quality (may be an older source version)", key, "warning")
        item["current_quality_reference_count"] = source_references[key] if item["tier"] == "bronze" else ""
        item["object_key"] = key
        item["sha256"] = item.get("observed_body_sha256_hex") or item.get("declared_sha256_hex", "")
    for check in head_checks:
        if check["status"] != "MATCH":
            issue(check.get("date_kst", ""), "SAMPLE_HEAD_MISMATCH", check["status"], check["object_key"])
    etag_groups = defaultdict(list)
    for item in objects.values():
        if item["etag"]:
            etag_groups[(item["size_bytes"], item["etag"])].append(item["key"])
    candidate_duplicates = [{"size_bytes": size, "etag": etag, "keys": keys,
                             "interpretation": "LIST ETag+size candidate only; not a full-content duplicate proof"}
                            for (size, etag), keys in sorted(etag_groups.items()) if len(keys) > 1]
    return sorted(objects.values(), key=lambda row: row["key"]), days, scenes, assets, issues, candidate_duplicates


def sample_heads(client, bucket, snapshot, fetched, offline, out):
    path = out / "sample_head_checks.json"
    if offline:
        if not path.is_file():
            raise ValueError("Offline snapshot has no sampled HEAD evidence")
        evidence = json.loads(path.read_text())
        if evidence.get("snapshot_sha256") != snapshot["snapshot_sha256"]:
            raise ValueError("Offline HEAD evidence belongs to another LIST snapshot")
        return evidence["checks"]
    markers = sorted(key for key in fetched if key.endswith("/completion.json"))
    selected = sorted({markers[0], markers[len(markers) // 2], markers[-1]}) if markers else []
    rows = []
    inventory = {row["key"]: row for row in snapshot["objects"]}
    for marker in selected:
        manifest = fetched[marker]["payload"]
        for tier in DAILY_NAMES:
            item = manifest.get("objects", {}).get(tier, {})
            key = item.get("key", "")
            try:
                head = client.head_object(Bucket=bucket, Key=key)
                metadata = head.get("Metadata", {})
                checks = {"size_match": head["ContentLength"] == item.get("size_bytes") == inventory.get(key, {}).get("size_bytes"),
                          "sha256_match": bool(sha(item.get("sha256"))) and sha(metadata.get("sha256")) == sha(item.get("sha256")),
                          "fingerprint_match": metadata.get("input-fingerprint") == manifest.get("input_fingerprint"),
                          "pipeline_version_match": metadata.get("pipeline-version") == manifest.get("pipeline_version"),
                          "list_etag_match": head.get("ETag", "").strip('"') == inventory.get(key, {}).get("etag")}
                rows.append({"date_kst": manifest.get("date_kst", ""), "tier": tier, "object_key": key,
                             **checks, "status": "MATCH" if all(checks.values()) else "MISMATCH"})
            except Exception as exc:
                rows.append({"date_kst": manifest.get("date_kst", ""), "tier": tier, "object_key": key, "status": error_code(exc)})
    write_json(path, {"snapshot_sha256": snapshot["snapshot_sha256"], "checks": rows})
    return rows


def latest_parquet_footers(client, bucket, snapshot, fetched, offline, out):
    """Read trailer and footer ranges only; statistics describe the sample file."""
    path = out / "latest_parquet_footers.json"
    if offline:
        if not path.is_file():
            raise ValueError("Offline snapshot has no footer evidence")
        evidence = json.loads(path.read_text())
        if evidence.get("snapshot_sha256") != snapshot["snapshot_sha256"]:
            raise ValueError("Offline footer evidence belongs to another LIST snapshot")
        return evidence["checks"]
    import pyarrow as pa
    import pyarrow.parquet as pq
    markers = sorted(key for key in fetched if key.endswith("/completion.json"))
    if not markers:
        return []
    manifest = fetched[markers[-1]]["payload"]
    inventory = {item["key"]: item for item in snapshot["objects"]}
    rows = []
    for tier in ("silver", "gold"):
        item = manifest.get("objects", {}).get(tier, {})
        key = item.get("key", "")
        try:
            listed = inventory[key]
            options = {"Bucket": bucket, "Key": key, "IfMatch": '"' + listed["etag"] + '"'}
            response = client.get_object(**options, Range="bytes=-8")
            with response["Body"] as stream:
                trailer = stream.read(9)
            if len(trailer) != 8 or trailer[4:] != b"PAR1":
                raise ValueError("Invalid Parquet trailer")
            length = int.from_bytes(trailer[:4], "little")
            if not 0 < length <= 2 * 1024 * 1024 or length + 8 >= listed["size_bytes"]:
                raise ValueError("Refusing invalid or oversized footer")
            response = client.get_object(**options, Range=f"bytes={listed['size_bytes'] - 8 - length}-{listed['size_bytes'] - 9}")
            with response["Body"] as stream:
                footer = stream.read(length + 1)
            if len(footer) != length:
                raise ValueError("Footer length mismatch")
            parquet = pq.ParquetFile(pa.BufferReader(b"PAR1" + footer + trailer))
            meta = parquet.metadata
            extrema = {}
            wanted = {"longitude", "latitude", "i05_brightness_temperature_k",
                      "temperature_min_k", "temperature_max_k", "temperature_mean_k",
                      "i05_quality_flags", "scan_start_time_tai93_s"}
            for group_index in range(meta.num_row_groups):
                group = meta.row_group(group_index)
                for column_index in range(group.num_columns):
                    column = group.column(column_index)
                    if column.path_in_schema not in wanted or not column.statistics or not column.statistics.has_min_max:
                        continue
                    stats = column.statistics
                    previous = extrema.get(column.path_in_schema)
                    extrema[column.path_in_schema] = {
                        "min": min(previous["min"], stats.min) if previous else stats.min,
                        "max": max(previous["max"], stats.max) if previous else stats.max,
                        "null_count": (previous["null_count"] if previous else 0) + (stats.null_count or 0),
                    }
            allowed_metadata = {"pipeline_version", "kst_date", "date_kst", "boundary_version",
                                "temperature_units", "units", "cloud_qc_status", "aggregation",
                                "temperature_valid_rule", "coordinate_crs", "sampling", "scan_time_units",
                                "scan_time_precision", "time_accuracy", "resampling", "input_fingerprint",
                                "processing_code_sha256", "boundary_sha256"}
            metadata = {key.decode(): value.decode() for key, value in (parquet.schema_arrow.metadata or {}).items()
                        if key.decode() in allowed_metadata}
            rows.append({"date_kst": manifest.get("date_kst"), "tier": tier, "object_key": key,
                         "status": "FOOTER_READ", "footer_bytes_downloaded": length + 8,
                         "full_object_bytes": listed["size_bytes"], "row_count": meta.num_rows,
                         "row_group_count": meta.num_row_groups,
                         "schema": [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in parquet.schema_arrow],
                         "metadata": metadata, "column_statistics_extrema": extrema,
                         "statistics_scope": "Parquet footer statistics of this single daily sample; not a full scan or all-date footprint"})
        except Exception as exc:
            rows.append({"date_kst": manifest.get("date_kst"), "tier": tier, "object_key": key,
                         "status": "READ_FAILED", "reason": error_code(exc)})
    write_json(path, {"snapshot_sha256": snapshot["snapshot_sha256"], "checks": rows})
    return rows


def main():
    args = arguments()
    args.out.mkdir(parents=True, exist_ok=True)
    server_rows, evidence = archived_server_report(args.server_archive, args.out)
    snapshot_path = args.out / "list_snapshot.json"
    client = bucket = None
    if args.offline:
        snapshot = json.loads(snapshot_path.read_text())
    else:
        client, bucket, prefix = client_config(args.workers)
        snapshot = list_snapshot(client, bucket, prefix)
        write_json(snapshot_path, snapshot)
    selected = []
    for item in snapshot["objects"]:
        parsed = parse_key(snapshot["prefix"], item["key"])
        if parsed and ((parsed[0] == "publication" and parsed[3] == "completion.json") or
                       (parsed[0] == "quality" and parsed[3] == "all-observations.json")):
            selected.append(item)
    fetched, failures = {}, {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = {pool.submit(fetch_small, client, bucket, item, args.cache, args.offline, args.refresh): item["key"] for item in selected}
        for task in as_completed(tasks):
            try:
                key, result = task.result()
                fetched[key] = result
            except Exception as exc:
                failures[tasks[task]] = error_code(exc)
    if failures:
        # Keep a preceding usable report intact if a fresh snapshot cannot be read.
        write_json(args.out / "audit_attempt_status.json", {
            "status": "incomplete", "snapshot_sha256": snapshot["snapshot_sha256"],
            "small_json_failures": len(failures), "failure_codes": dict(Counter(failures.values())),
            "note": "CSV/summary reports were not replaced by this incomplete attempt",
        })
        print(json.dumps({"status": "incomplete", "small_json_failures": len(failures)}))
        return 2
    heads = sample_heads(client, bucket, snapshot, fetched, args.offline, args.out)
    footers = latest_parquet_footers(client, bucket, snapshot, fetched, args.offline, args.out)
    objects, days, scenes, assets, issues, duplicates = audit(snapshot, fetched, failures, server_rows, heads)
    by_day = {row["date_kst"]: row for row in days}
    for footer in footers:
        if footer["status"] != "FOOTER_READ":
            issues.append({"date_kst": footer.get("date_kst", ""), "severity": "warning", "code": "LATEST_FOOTER_UNAVAILABLE",
                           "object_key": footer["object_key"], "detail": footer.get("reason", "unknown")})
            continue
        day_record = by_day.get(footer["date_kst"], {})
        expected_field = "pixel_count" if footer["tier"] == "silver" else "gold_rows_recorded_server"
        if str(footer["row_count"]) != str(day_record.get(expected_field)):
            issues.append({"date_kst": footer["date_kst"], "severity": "error", "code": "LATEST_FOOTER_ROW_COUNT_MISMATCH",
                           "object_key": footer["object_key"], "detail": expected_field})
    monthly = []
    for month in sorted({row["date_kst"][:7] for row in days}):
        group = [row for row in days if row["date_kst"].startswith(month)]
        monthly.append({"month_kst": month, "calendar_days": len(group),
                        "published_days": sum(row["status"] == "CONSISTENT_METADATA_AND_QUALITY" for row in group),
                        "no_paired_cmr_days": sum(row["status"] == "NO_PAIRED_CMR" for row in group),
                        "scene_count": sum(int(row.get("scene_count") or 0) for row in group),
                        **{name: sum(int(row.get(name) or 0) for row in group) for name in COUNTS},
                        "gold_rows_recorded_server": sum(int(row.get("gold_rows_recorded_server") or 0) for row in group),
                        "cloud_qc_status": "unverified"})
    outputs = {"objects.csv": objects, "days.csv": days, "scenes.csv": scenes,
               "source_assets.csv": assets, "issues.csv": issues, "monthly_summary.csv": monthly}
    for name, rows in outputs.items():
        columns = list(dict.fromkeys(key for row in rows for key in row))
        if name == "objects.csv":
            columns = ["object_key", "tier", "date_kst", "size_bytes", "etag", "sha256"] + [key for key in columns if key not in ("key", "object_key", "tier", "date_kst", "size_bytes", "etag", "sha256")]
        write_csv(args.out / name, rows, columns or ("date_kst", "severity", "code", "object_key", "detail"))
    write_json(args.out / "duplicate_candidates.json", duplicates)
    lineages = {}
    for key, record in fetched.items():
        if not key.endswith("/all-observations.json"):
            continue
        quality = record["payload"]
        code_sha = quality.get("processing_code_sha256", "")
        boundary_version = quality.get("boundary_version", "")
        lineage_key = code_sha + ":" + boundary_version
        if lineage_key not in lineages:
            lineages[lineage_key] = {
                "pipeline_version": quality.get("pipeline_version", ""),
                "processing_code_sha256": code_sha,
                "processing_code_hashes": {name: value for name, value in quality.get("processing_code_hashes", {}).items()
                                           if re.fullmatch(r"[A-Za-z0-9_.-]+\.py", name) and re.fullmatch(r"[a-fA-F0-9]{64}", str(value))},
                "boundary_version": boundary_version, "boundary_sha256": quality.get("boundary_sha256", ""),
                "date_count": 0,
            }
        lineages[lineage_key]["date_count"] += 1
    write_json(args.out / "lineage_versions.json", [lineages[key] for key in sorted(lineages)])
    unique_scenes = {(row["date_kst"], row["swath_id"]) for row in scenes}
    issue_counts = Counter(row["code"] for row in issues)
    severity_counts = Counter(row["severity"] for row in issues)
    summary = {
        "source": "Existing VIIRS R2 publication; retired-server safe daily reports",
        "captured_at_utc": snapshot["captured_at_utc"], "snapshot_sha256": snapshot["snapshot_sha256"],
        "period_kst": {"start_inclusive": "2024-01-01", "stop_exclusive": "2026-01-01", "calendar_days": 731},
        "object_count": len(objects), "object_bytes": sum(row["size_bytes"] for row in objects),
        "objects_with_known_sha256": sum(bool(row.get("sha256")) for row in objects),
        "object_tier_counts": dict(Counter(row["tier"] for row in objects)),
        "object_tier_bytes": dict(Counter({tier: sum(row["size_bytes"] for row in objects if row["tier"] == tier) for tier in sorted({row["tier"] for row in objects})})),
        "daily_status_counts": dict(Counter(row["status"] for row in days)),
        "scene_records": len(scenes), "unique_date_swath_records": len(unique_scenes),
        "duplicate_date_swath_records": len(scenes) - len(unique_scenes),
        "zero_land_pixel_scenes": sum(int(row["pixel_count"]) == 0 for row in scenes),
        "zero_valid_temperature_scenes": sum(int(row["valid_flag_zero_temperature"]) == 0 for row in scenes),
        "referenced_source_asset_rows": len(assets), "unique_referenced_source_keys": len({row["object_key"] for row in assets}),
        "quality_totals": {name: sum(int(row.get(name) or 0) for row in scenes) for name in COUNTS},
        "gold_rows_recorded_server": sum(int(row.get("gold_rows") or 0) for row in server_rows),
        "source_unit_check_counts": dict(Counter(row["unit_check"] for row in scenes)),
        "source_platforms": sorted({row["platform"] for row in scenes}),
        "source_instruments": sorted({row["instrument"] for row in scenes}),
        "pipeline_versions": sorted({row["pipeline_version"] for row in scenes}),
        "processing_boundary_lineage_groups": len(lineages),
        "new_collection_or_processing_performed": False,
        "no_paired_cmr_dates": evidence["no_paired_cmr_dates"],
        "small_json_objects_checked": len(fetched), "small_json_failure_count": len(failures),
        "small_json_retrieval_counts": dict(Counter(row["retrieval"] for row in fetched.values())),
        "list_pages": snapshot["list_pages"], "sample_head_objects_checked": len(heads),
        "sample_head_status_counts": dict(Counter(row["status"] for row in heads)),
        "latest_parquet_footer_status_counts": dict(Counter(row["status"] for row in footers)),
        "issue_counts": dict(sorted(issue_counts.items())), "issue_severity_counts": dict(severity_counts),
        "etag_size_duplicate_candidate_groups": len(duplicates),
        "cloud_qc_status": "unverified", "measured_spatial_bbox": None,
        "query_bbox_wsen": [124, 32, 132, 39], "query_bbox_crs": "EPSG:4326",
        "check_level": {
            "all_objects": "Paginated LIST key/size/ETag/last-modified snapshot",
            "all_publications": "Completion paths/sizes/valid digest declarations versus LIST; date and lineage checks",
            "all_quality": "Small JSON GET (If-Match), actual SHA-256 versus manifest and GET metadata, per-scene sums and QA/unit/time checks",
            "silver_gold": "LIST size versus manifest; row counts from archived per-day reports; SHA metadata/lineage HEAD sampled on earliest/middle/latest published days",
            "bronze": "Referenced filenames exist in LIST; source hashes and units from processing quality records, not rehashed raw files",
            "latest_day_footer_sample": "Range-only Parquet metadata for actual schema/row counts/statistics; no full Parquet download",
            "not_performed": "Full NetCDF/Parquet downloads, Parquet unique-pixel/content revalidation, new CMR queries, cloud/land coverage validation, processing, collection, or remote writes",
        },
        "limitations": [
            "CONSISTENT_METADATA_AND_QUALITY does not mean full-byte Silver/Gold validation or scientific QA approval.",
            "NO_PAIRED_CMR is conditional on the original product-pair/search policy; missing days are not zero-valued observations.",
            "CMR search BBOX is not an observed swath/pixel footprint. Native geolocated pixel centers were assigned to annual SGIS administrative polygons; no resampling.",
            "Finite brightness temperature with source QA=0 is the current Gold inclusion rule; it does not certify cloud-free measurements or physical range validity.",
            "Gold rows are dong x swath groups. Pixel totals include repeated observations and are not unique ground area.",
            "Granule UTC and KST times are source coverage metadata; scan timestamps are TAI93 scan starts, not exact pixel exposure times.",
            "No rainfall ground truth, heavy-rain labels, or Sentinel/NGII integration is inferred.",
        ],
    }
    write_json(args.out / "summary.json", summary)
    write_json(args.out / "audit_attempt_status.json", {
        "status": "complete", "snapshot_sha256": snapshot["snapshot_sha256"],
        "small_json_objects_checked": len(fetched), "errors": severity_counts.get("error", 0),
    })
    print(json.dumps({"status": "review" if severity_counts.get("error") else "consistent",
                      "objects": len(objects), "published_days": sum(row["status"] == "CONSISTENT_METADATA_AND_QUALITY" for row in days),
                      "scenes": len(scenes), "errors": severity_counts.get("error", 0),
                      "warnings": severity_counts.get("warning", 0), "out": str(args.out)}, ensure_ascii=False))
    return 1 if severity_counts.get("error") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Exception text can include account-specific endpoints or credentials.
        print(json.dumps({"status": "error", "reason": error_code(exc)}), file=sys.stderr)
        raise SystemExit(2)
