#!/usr/bin/env python3
"""Read-only audit of VIIRS Bronze, Silver, Quality and Gold in Cloudflare R2.

Examples:
  python viirs_r2_audit.py --bucket YOUR_BUCKET --account-id YOUR_R2_ACCOUNT_ID
  python viirs_r2_audit.py --bucket YOUR_BUCKET --account-id YOUR_R2_ACCOUNT_ID --deep

Requires: boto3, python-dotenv; for --deep, also pyarrow.
The program never issues PUT, POST, DELETE, or COPY requests to R2.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

DEFAULT_BOUNDARIES = {2024: "sgis-2024-2q", 2025: "sgis-2025-2q"}
DATE_PART = re.compile(r"^([0-9]{4})/([0-9]{2})/([0-9]{2})/(.+)$")
HASH_KEYS = {"sha256", "content-sha256", "file-sha256", "md5", "content-md5", "file-md5"}
SCENE_FILE = re.compile(r"^A[0-9]{7}\.[0-9]{4}$")
BRONZE_FILE = re.compile(r"^(VNP02IMG|VNP03IMG)\.(A[0-9]{7}\.[0-9]{4})\..+\.nc$")
DAILY_FILES = {"silver": "all-observations.parquet", "quality": "all-observations.json",
               "gold": "dong-observations.parquet"}


def env_first(*names: str) -> str | None:
    return next((v for name in names if (v := os.getenv(name))), None)


def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=".env", help="local dotenv file, default: .env")
    p.add_argument("--bucket", help="R2 bucket (or R2_BUCKET in .env)")
    p.add_argument("--account-id", help="R2 account ID (or R2_ACCOUNT_ID in .env)")
    p.add_argument("--endpoint-url", help="optional complete S3 endpoint URL")
    p.add_argument("--prefix", default="viirs-daily", help="object prefix, default: viirs-daily")
    p.add_argument("--years", nargs="+", type=int, default=[2024, 2025])
    p.add_argument("--start", type=date.fromisoformat, help="First KST date (inclusive)")
    p.add_argument("--stop", type=date.fromisoformat, help="Last KST date (exclusive)")
    p.add_argument("--boundary-2024", default=DEFAULT_BOUNDARIES[2024])
    p.add_argument("--boundary-2025", default=DEFAULT_BOUNDARIES[2025])
    p.add_argument("--out", default="r2_audit_report", help="local report directory")
    p.add_argument("--workers", type=int, default=8, help="concurrent HEAD requests")
    p.add_argument("--deep", action="store_true", help="download derived objects to temporary local files and validate content")
    p.add_argument("--deep-bronze", action="store_true", help="also stream Bronze objects to validate hashes if stored (large traffic)")
    return p.parse_args()


def make_client(args: argparse.Namespace):
    access = env_first("R2_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret = env_first("R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    bucket = args.bucket or env_first("R2_BUCKET", "R2_BUCKET_NAME", "R2_BUCKET_ID", "S3_BUCKET", "BUCKET")
    account = args.account_id or env_first("R2_ACCOUNT_ID", "CLOUDFLARE_R2_ACCOUNT_ID")
    endpoint = args.endpoint_url or env_first("R2_ENDPOINT_URL", "R2_ENDPOINT")
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    missing = [n for n, v in (("R2_ACCESS_KEY_ID", access), ("R2_SECRET_ACCESS_KEY", secret),
                             ("bucket (--bucket or R2_BUCKET)", bucket),
                             ("account ID (--account-id or R2_ACCOUNT_ID), or --endpoint-url", endpoint)) if not v]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing) +
                         ". The key/secret must be R2 S3 API credentials, not a Cloudflare Bearer API token.")
    if not endpoint.startswith("https://"):
        raise ValueError("R2 endpoint must be an https:// URL")
    config = Config(region_name="auto", retries={"mode": "standard", "max_attempts": 5},
                    connect_timeout=20, read_timeout=90,
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    s3={"addressing_style": "path"}, max_pool_connections=max(10, args.workers + 2))
    return boto3.client("s3", endpoint_url=endpoint, region_name="auto", config=config,
                        aws_access_key_id=access, aws_secret_access_key=secret), bucket


def parse_key(prefix: str, key: str):
    """Returns (tier, boundary, date, filename), or None for non-standard keys."""
    root = prefix.rstrip("/") + "/"
    if not key.startswith(root):
        return None
    parts = key[len(root):].split("/", 2)
    if len(parts) < 2:
        return None
    tier = parts[0]
    if tier == "bronze" and len(parts) == 3:
        version, tail = None, parts[1] + "/" + parts[2]
    elif tier in ("silver", "quality", "gold") and len(parts) == 3:
        version, tail = parts[1], parts[2]
    else:
        return None
    matched = DATE_PART.fullmatch(tail)
    if not matched:
        return None
    year, month, day, filename = matched.groups()
    try:
        dt = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if not filename or "/" in filename:
        return None
    if tier == "quality" and filename == "completion.json":
        tier = "publication"
    return tier, version, dt, filename


def iterate_keys(client, bucket: str, prefix: str):
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):
                yield {"Key": obj["Key"], "Size": int(obj.get("Size", 0)),
                       "ETag": obj.get("ETag", "").strip('"')}


def head_item(client, bucket: str, obj: dict[str, Any]):
    key = obj["Key"]
    try:
        info = client.head_object(Bucket=bucket, Key=key)
        return key, {"meta": {str(k).lower(): str(v) for k, v in info.get("Metadata", {}).items()},
                     "head_size": int(info.get("ContentLength", -1)),
                     "head_etag": info.get("ETag", "").strip('"'),
                     "content_type": info.get("ContentType", ""), "head_error": ""}
    except (ClientError, BotoCoreError, OSError) as exc:
        return key, {"meta": {}, "head_size": None, "content_type": "", "head_error": str(exc)}


def issue(issues: list[dict[str, str]], day: date | str, category: str, msg: str, key: str = "") -> None:
    issues.append({"date": str(day), "category": category, "key": key, "detail": msg})


def decode_digest(expected: str, algorithm: str) -> bytes:
    """Both original R2 uploads and rebuilds store base64 SHA-256 metadata."""
    length = 32 if algorithm == "sha256" else 16
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % (length * 2), expected):
        return bytes.fromhex(expected)
    try:
        digest = base64.b64decode(expected, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError(f"Invalid {algorithm} metadata encoding") from exc
    if len(digest) != length:
        raise ValueError(f"Invalid {algorithm} metadata length")
    return digest


def content_check(client, bucket: str, obj: dict[str, Any], kind: str) -> dict[str, Any]:
    """Read-only GET, local temporary file; not a full semantic VIIRS quality assessment."""
    key = obj["Key"]
    meta = obj.get("meta", {})
    options = {"IfMatch": obj.get("head_etag") or obj.get("ETag")} if obj.get("head_etag") or obj.get("ETag") else {}
    response = client.get_object(Bucket=bucket, Key=key, **options)
    digests = {"md5": hashlib.md5(), "sha256": hashlib.sha256()}
    received = 0
    with tempfile.TemporaryDirectory(prefix="viirs-audit-") as td:
        name = "check.parquet" if kind in ("silver", "gold") else "check.json" if kind in ("quality", "publication") else "check.bin"
        path = Path(td) / name
        try:
            with path.open("wb") as output:
                for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    received += len(chunk)
                    for digest in digests.values():
                        digest.update(chunk)
        finally:
            response["Body"].close()
        if received != obj["Size"]:
            raise ValueError(f"content bytes {received} != listed size {obj['Size']}")
        for meta_key, expected in meta.items():
            if meta_key not in HASH_KEYS:
                continue
            algo = "sha256" if "sha256" in meta_key else "md5"
            if digests[algo].digest() != decode_digest(expected, algo):
                raise ValueError(f"content {algo} hash does not match user metadata")
        result: dict[str, Any] = {"downloaded_bytes": received,
                                  "sha256": base64.b64encode(digests["sha256"].digest()).decode("ascii")}
        if kind in ("quality", "publication"):
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                raise ValueError(f"{kind} JSON is not an object")
            result["json_fields"] = sorted(payload.keys())
            result["payload"] = payload
        elif kind in ("silver", "gold"):
            import pyarrow.parquet as pq
            parquet = pq.ParquetFile(path)
            rows = 0
            totals = Counter()
            swaths = set()
            for batch in parquet.iter_batches(batch_size=32768):
                rows += batch.num_rows
                if "swath_id" in batch.schema.names:
                    values = batch.column(batch.schema.get_field_index("swath_id")).unique().to_pylist()
                    if any(not isinstance(value, str) or not SCENE_FILE.fullmatch(value) for value in values):
                        raise ValueError("Daily observation contains a missing/invalid swath_id")
                    swaths.update(values)
                if kind == "gold":
                    for column in ("pixel_count", "valid_count"):
                        if column in batch.schema.names:
                            values = batch.column(batch.schema.get_field_index(column)).to_pylist()
                            if any(value is None or not isinstance(value, int) or value < 0 for value in values):
                                raise ValueError(f"Gold {column} must contain nonnegative integer counts")
                            totals[column] += sum(values)
            result["row_count"] = rows
            result["columns"] = parquet.schema_arrow.names
            if "swath_id" in parquet.schema_arrow.names:
                result["swaths"] = sorted(swaths)
            if kind == "gold":
                result["count_totals"] = dict(totals)
            # Empty land intersections are valid observations. Cross-check their
            # declared pixel count/status against Quality rather than invent data.
        return result


def validate_daily(day: date, bronze: list[dict], silver: list[dict], quality: list[dict],
                   gold: list[dict], issues: list[dict[str, str]]) -> bool:
    """Validate either legacy per-scene outputs or the all-observations daily set.

    This validates stored composition, not the completeness of the CMR selection.
    Returns whether the daily all-observations format is present.
    """
    daily = any(Path(obj["Key"]).name == DAILY_FILES[kind]
                for kind, coll in (("silver", silver), ("quality", quality), ("gold", gold)) for obj in coll)
    pairs: dict[str, set[str]] = defaultdict(set)
    for obj in bronze:
        name = Path(obj["Key"]).name
        matched = BRONZE_FILE.fullmatch(name)
        if not matched:
            issue(issues, day, "BRONZE_FILENAME", "Unrecognized original product filename", obj["Key"])
        else:
            product, scene = matched.groups()
            pairs[scene].add(product)
        if not obj.get("head_error") and obj.get("meta", {}).get("nasa-granule") != name:
            issue(issues, day, "BRONZE_METADATA", "nasa-granule metadata missing or mismatched", obj["Key"])
    if not pairs or any(products != {"VNP02IMG", "VNP03IMG"} for products in pairs.values()):
        issue(issues, day, "BRONZE_PAIRING", "Expected matching VNP02IMG and VNP03IMG original pairs")

    if daily:
        for kind, coll in (("silver", silver), ("quality", quality), ("gold", gold)):
            if len(coll) != 1 or Path(coll[0]["Key"]).name != DAILY_FILES[kind]:
                issue(issues, day, f"{kind.upper()}_COMPOSITION", f"Expected only {DAILY_FILES[kind]}; found {len(coll)} objects")
        products = silver + quality + gold
        fingerprints = {obj.get("meta", {}).get("input-fingerprint") for obj in products}
        versions = {obj.get("meta", {}).get("pipeline-version") for obj in products}
        if len(fingerprints) != 1 or not all(fingerprints):
            issue(issues, day, "DAILY_INPUT_MISMATCH", "Daily outputs have missing or differing input fingerprints")
        if len(versions) != 1 or not all(versions):
            issue(issues, day, "DAILY_VERSION_MISMATCH", "Daily outputs have missing or differing pipeline versions")
    else:
        if not silver or not quality:
            issue(issues, day, "DERIVED_MISSING", "Expected Silver and Quality outputs for the selected observations")
        by_kind = {}
        bronze_names = {Path(obj["Key"]).name for obj in bronze}
        for kind, coll, suffix in (("silver", silver, ".parquet"), ("quality", quality, ".json")):
            by_kind[kind] = {}
            for obj in coll:
                md = obj.get("meta", {})
                scene = Path(obj["Key"]).stem
                by_kind[kind][scene] = md
                if not SCENE_FILE.fullmatch(scene) or not obj["Key"].endswith(suffix):
                    issue(issues, day, "LEGACY_FILENAME", f"Expected scene identifier and {suffix}", obj["Key"])
                for name in ("swath", "i05-granule", "geolocation-granule", "boundary-sha256"):
                    if not obj.get("head_error") and not md.get(name):
                        issue(issues, day, "MISSING_METADATA", f"Missing {name}", obj["Key"])
                if md.get("swath") and md["swath"] != scene:
                    issue(issues, day, "SWATH_MISMATCH", "Object filename does not match swath metadata", obj["Key"])
                for name in ("i05-granule", "geolocation-granule"):
                    if md.get(name) and md[name] not in bronze_names:
                        issue(issues, day, "BRONZE_REFERENCE", f"Absent Bronze referenced by {name}", obj["Key"])
                if kind == "quality" and md.get("report-version") != "1" and not obj.get("head_error"):
                    issue(issues, day, "REPORT_VERSION", "Expected legacy report-version=1", obj["Key"])
        if set(by_kind["silver"]) != set(by_kind["quality"]):
            issue(issues, day, "SILVER_QUALITY_PAIRING", "Silver and Quality scene identifiers differ")
        for scene in set(by_kind["silver"]) & set(by_kind["quality"]):
            for name in ("swath", "i05-granule", "geolocation-granule", "boundary-sha256"):
                if by_kind["silver"][scene].get(name) != by_kind["quality"][scene].get(name):
                    issue(issues, day, "SILVER_QUALITY_MISMATCH", f"Metadata differs for {scene}: {name}")
    return daily


def validate_publication(client, bucket: str, day: date, version: str, publications: list[dict],
                         derived: dict[str, list[dict]], issues: list[dict[str, str]]) -> None:
    """Publication is verified against current HEADs, never just marker existence."""
    if len(publications) != 1:
        issue(issues, day, "PUBLICATION_COUNT", "Expected one completion.json")
        return
    marker = publications[0]
    if marker["Size"] > 1024 * 1024:
        issue(issues, day, "PUBLICATION_SIZE", "Completion marker exceeds 1 MiB", marker["Key"])
        return
    try:
        manifest = content_check(client, bucket, marker, "publication")["payload"]
        if (manifest.get("manifest_version") != 1 or manifest.get("date_kst") != str(day)
                or manifest.get("boundary_version") != version):
            raise ValueError("Completion marker version/date/boundary does not match its path")
        entries = manifest.get("objects")
        if not isinstance(entries, dict) or set(entries) != set(DAILY_FILES):
            raise ValueError("Completion marker must reference Silver, Quality and Gold")
        fingerprint = manifest.get("input_fingerprint")
        pipeline_version = manifest.get("pipeline_version")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint or "") or not pipeline_version:
            raise ValueError("Completion marker lacks input fingerprint/pipeline version")
        for kind, coll in derived.items():
            if len(coll) != 1:
                raise ValueError(f"Completion marker cannot verify ambiguous {kind} composition")
            obj, entry = coll[0], entries[kind]
            if not isinstance(entry, dict):
                raise ValueError("Invalid completion object entry")
            md = obj.get("meta", {})
            if (entry.get("key") != obj["Key"] or entry.get("size_bytes") != obj.get("head_size")
                    or entry.get("sha256") != md.get("sha256")
                    or md.get("input-fingerprint") != fingerprint
                    or md.get("pipeline-version") != pipeline_version):
                raise ValueError(f"Completion marker differs from current {kind} HEAD")
            decode_digest(entry.get("sha256", ""), "sha256")
    except Exception as exc:
        issue(issues, day, "PUBLICATION_FAILED", f"{type(exc).__name__}: {exc}", marker["Key"])


def validate_deep_day(day: date, daily: bool, silver: list[dict], quality: list[dict],
                      gold: list[dict], bronze: list[dict], results: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Cross-check record counts, daily source identities and empty observations."""
    silver_rows = {Path(obj["Key"]).stem: results[obj["Key"]]["row_count"]
                   for obj in silver if obj["Key"] in results}
    for obj in quality:
        checked = results.get(obj["Key"])
        if not checked:
            continue
        payload = checked["payload"]
        stem = Path(obj["Key"]).stem
        if stem in silver_rows and payload.get("pixel_count") != silver_rows[stem]:
            issue(issues, day, "PIXEL_COUNT_MISMATCH", "Quality pixel_count differs from Silver row count", obj["Key"])
        if not daily:
            continue
        expected = {"dong_code", "row", "column", "longitude", "latitude",
                    "i05_brightness_temperature_k", "i05_quality_flags", "swath_id",
                    "swath_start_utc", "scan_index", "scan_start_time_tai93_s"}
        for source in silver:
            check = results.get(source["Key"])
            if check and not expected.issubset(check.get("columns", [])):
                issue(issues, day, "SILVER_SCHEMA", "Daily Silver lacks source, location, temperature or scan-time columns", source["Key"])
        if payload.get("date_kst") != str(day):
            issue(issues, day, "QUALITY_DATE", "Daily Quality date_kst does not match its path", obj["Key"])
        if payload.get("boundary_version") != obj["parsed"][1]:
            issue(issues, day, "QUALITY_BOUNDARY", "Daily Quality boundary version does not match its path", obj["Key"])
        per_scene = payload.get("per_scene")
        if (not isinstance(per_scene, dict) or not per_scene
                or payload.get("source_scene_count") != len(per_scene)):
            issue(issues, day, "QUALITY_SCENES", "Daily Quality scene count/per_scene disagree", obj["Key"])
        elif (any(not isinstance(item, dict) or not isinstance(item.get("pixel_count"), int)
                  or item["pixel_count"] < 0 for item in per_scene.values())
              or sum(item["pixel_count"] for item in per_scene.values()) != payload.get("pixel_count")):
            issue(issues, day, "QUALITY_SCENE_PIXELS", "Daily per-scene pixel counts do not sum to total", obj["Key"])
        if isinstance(per_scene, dict):
            bronze_scenes = {match[2] for source in bronze
                             if (match := BRONZE_FILE.fullmatch(Path(source["Key"]).name))}
            if not set(per_scene).issubset(bronze_scenes):
                issue(issues, day, "QUALITY_BRONZE_REFERENCE", "Daily Quality references scenes absent from Bronze", obj["Key"])
            silver_swaths = {scene for source in silver for scene in results.get(source["Key"], {}).get("swaths", [])}
            gold_swaths = {scene for source in gold for scene in results.get(source["Key"], {}).get("swaths", [])}
            if not silver_swaths.issubset(per_scene) or gold_swaths != silver_swaths:
                issue(issues, day, "OBSERVATION_IDENTITY", "Silver/Gold scene identities do not agree with Quality", obj["Key"])
        if payload.get("pixel_count") == 0 and payload.get("status") != "EMPTY_LAND_OBSERVATION":
            issue(issues, day, "EMPTY_STATUS", "Zero-row daily observation lacks EMPTY_LAND_OBSERVATION status", obj["Key"])
        if payload.get("pixel_count") == 0:
            for source in gold:
                if results.get(source["Key"], {}).get("row_count", 0) != 0:
                    issue(issues, day, "EMPTY_GOLD", "Empty Silver has nonempty Gold", source["Key"])
        for source in gold:
            check = results.get(source["Key"])
            if not check:
                continue
            if not {"dong_code", "swath_id", "swath_start_utc", "pixel_count", "valid_count"}.issubset(check["columns"]):
                issue(issues, day, "GOLD_SCHEMA", "Daily Gold lacks observation identities/counts", source["Key"])
            elif (check["count_totals"].get("pixel_count", 0) != payload.get("pixel_count")
                  or check["count_totals"].get("valid_count", 0) != payload.get("valid_flag_zero_temperature")):
                issue(issues, day, "GOLD_COUNTS", "Gold group pixel/valid counts do not equal daily Quality totals", source["Key"])


def audit(args: argparse.Namespace) -> int:
    load_dotenv(args.env, override=False)
    client, bucket = make_client(args)
    prefix = args.prefix.strip("/")
    if not prefix:
        raise ValueError("--prefix cannot be empty")
    years = sorted(set(args.years))
    if not years or any(year not in DEFAULT_BOUNDARIES for year in years):
        raise ValueError("Only years 2024 and 2025 are configured; adjust boundary options/code for other years")
    if args.workers < 1 or args.workers > 32:
        raise ValueError("--workers must be between 1 and 32")
    versions = {2024: args.boundary_2024, 2025: args.boundary_2025}
    if args.deep_bronze and not args.deep:
        raise ValueError("--deep-bronze requires --deep")
    if args.deep:
        try:
            import pyarrow.parquet  # noqa: F401
        except ImportError as exc:
            raise ValueError("--deep needs pyarrow: python -m pip install pyarrow") from exc

    audit_start = args.start or date(min(years), 1, 1)
    audit_stop = args.stop or date(max(years) + 1, 1, 1)
    if audit_start >= audit_stop:
        raise ValueError("--start must precede --stop (exclusive)")
    selected_days = [date(year, 1, 1) + timedelta(days=offset)
                     for year in years
                     for offset in range((date(year + 1, 1, 1) - date(year, 1, 1)).days)
                     if audit_start <= date(year, 1, 1) + timedelta(days=offset) < audit_stop]
    if not selected_days:
        raise ValueError("Selected date range does not overlap --years")
    selected_set = set(selected_days)

    print(f"[1/4] Listing R2 objects under {prefix}/ (read-only)...", flush=True)
    objects: dict[str, dict[str, Any]] = {}
    any_listed = False
    for tier in ("bronze", "silver", "quality", "gold"):
        # Small focused audits avoid listing every day/object in the whole archive.
        locations = ([f"{prefix}/{tier}/" +
                      ("" if tier == "bronze" else f"{versions[day.year]}/") +
                      day.strftime("%Y/%m/%d/") for day in selected_days]
                     if len(selected_days) <= 31 else [f"{prefix}/{tier}/"])
        count = 0
        for location in locations:
            for obj in iterate_keys(client, bucket, location):
                any_listed = True
                obj["parsed"] = parse_key(prefix, obj["Key"])
                if obj["parsed"] is not None and obj["parsed"][2] not in selected_set:
                    continue
                objects[obj["Key"]] = obj
                count += 1
        print(f"    {tier}: {count} objects in scope", flush=True)
    if not any_listed:
        raise RuntimeError("No objects found in the selected scope. Check configuration/date range; "
                           "refusing to label an unverified configuration as all missing.")

    print(f"[2/4] HEAD-checking {len(objects)} objects...", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {pool.submit(head_item, client, bucket, obj): obj["Key"] for obj in objects.values()}
        for future in as_completed(future_map):
            key, result = future.result()
            objects[key].update(result)

    groups: dict[tuple[str, str | None, date], list[dict[str, Any]]] = defaultdict(list)
    extra_keys: list[str] = []
    for obj in objects.values():
        parsed = obj["parsed"]
        if parsed is None:
            extra_keys.append(obj["Key"])
            continue
        tier, version, dt, _ = parsed
        groups[(tier, version, dt)].append(obj)

    issues: list[dict[str, str]] = []
    dates: list[dict[str, Any]] = []
    deep_results: dict[str, Any] = {}
    print("[3/4] Checking daily composition, integrity and publication...", flush=True)
    for current in selected_days:
        version = versions[current.year]
        b = groups.get(("bronze", None, current), [])
        s = groups.get(("silver", version, current), [])
        q = groups.get(("quality", version, current), [])
        g = groups.get(("gold", version, current), [])
        publication = groups.get(("publication", version, current), [])
        all_objects = b + s + q + g + publication
        day_issues: list[dict[str, str]] = []
        daily = False
        if not all_objects:
            status = "UNVERIFIED_NO_OBJECTS"
            issue(day_issues, current, "NO_OBJECTS", "No objects for this date; compare the current CMR query before calling ingestion missing")
        else:
            daily = validate_daily(current, b, s, q, g, day_issues) or bool(publication)
            for obj in all_objects:
                if obj["Size"] <= 0:
                    issue(day_issues, current, "ZERO_BYTES", "Empty object", obj["Key"])
                if obj.get("head_error"):
                    issue(day_issues, current, "HEAD_FAILED", obj["head_error"], obj["Key"])
                elif obj.get("head_size") != obj["Size"]:
                    issue(day_issues, current, "SIZE_MISMATCH", "LIST and HEAD sizes differ", obj["Key"])
                elif obj.get("ETag") and obj.get("head_etag") != obj["ETag"]:
                    issue(day_issues, current, "OBJECT_CHANGED", "Object changed between LIST and HEAD; rerun after the writer finishes", obj["Key"])
                md = obj.get("meta", {})
                if not md.get("sha256"):
                    issue(day_issues, current, "CHECKSUM_MISSING", "No stored SHA-256; content identity cannot be verified", obj["Key"])
                else:
                    try:
                        decode_digest(md["sha256"], "sha256")
                    except ValueError as exc:
                        issue(day_issues, current, "CHECKSUM_ENCODING", str(exc), obj["Key"])
            if daily and publication:
                validate_publication(client, bucket, current, version, publication,
                                     {"silver": s, "quality": q, "gold": g}, day_issues)
            if args.deep:
                for obj in s + q + g + (b if args.deep_bronze else []):
                    if obj.get("head_error") or obj["Size"] <= 0:
                        continue
                    try:
                        deep_results[obj["Key"]] = content_check(client, bucket, obj, obj["parsed"][0])
                    except Exception as exc:
                        issue(day_issues, current, "CONTENT_FAILED", f"{type(exc).__name__}: {exc}", obj["Key"])
                validate_deep_day(current, daily, s, q, g, b, deep_results, day_issues)
            status = "FAIL" if day_issues else "PASS"
            if daily and not publication:
                if status == "PASS":
                    status = "UNVERIFIED_PUBLICATION"
                issue(day_issues, current, "PUBLICATION_MISSING", "Daily triplet has no completion.json; inspectable but not verified as a committed publication")
        issues.extend(day_issues)
        dates.append({"date": current.isoformat(), "year": current.year, "boundary_version": version,
                      "format": "all-observations" if daily else "legacy-per-scene" if all_objects else "none",
                      "status": status, "bronze_objects": len(b), "silver_objects": len(s),
                      "quality_objects": len(q), "gold_objects": len(g), "publication_objects": len(publication),
                      "issue_count": len(day_issues), "issues": ";".join(sorted({i["category"] for i in day_issues}))})

    for key in extra_keys:
        issue(issues, "outside-layout", "UNRECOGNIZED_KEY", "Unexpected path under scanned data prefixes", key)
    for (tier, version, dt), members in groups.items():
        if dt.year in years and tier in ("silver", "quality", "gold", "publication") and version != versions[dt.year]:
            for obj in members:
                issue(issues, dt, "OTHER_BOUNDARY_VERSION", f"Found additional {tier} object under boundary version {version}", obj["Key"])

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    date_cols = ["date", "year", "boundary_version", "format", "status", "bronze_objects", "silver_objects",
                 "quality_objects", "gold_objects", "publication_objects", "issue_count", "issues"]
    with (out / "daily_status.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=date_cols)
        writer.writeheader()
        writer.writerows(dates)
    with (out / "issues.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "category", "key", "detail"])
        writer.writeheader()
        writer.writerows(issues)
    statuses = Counter(d["status"] for d in dates)
    kinds = Counter(i["category"] for i in issues)
    summary = {"source": "Cloudflare R2", "bucket": bucket, "prefix": prefix,
               "years": years, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
               "start_kst": str(audit_start), "stop_kst_exclusive": str(audit_stop), "boundary_versions": {str(y): versions[y] for y in years},
               "mode": "deep" if args.deep else "inventory + HEAD only",
               "dates_checked": len(dates), "date_status_counts": dict(statuses),
               "object_count": len(objects), "issue_counts": dict(kinds),
               "notes": ["No satellite CMR query: empty dates are UNVERIFIED, not proven missing.",
                         "Inventory + HEAD cannot prove that object contents are undamaged.",
                         "For full contents verification, rerun with --deep; Bronze requires --deep-bronze.",
                         "CMR selection completeness, land coverage and sensor/cloud QA require separate checks.",
                         "A PASS in inventory mode verifies stored composition/metadata only, never scientific quality.",
                         "Daily files without a completion marker are UNVERIFIED_PUBLICATION, not a committed daily triplet.",
                         "R2 data was never modified by this program."]}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if deep_results:
        (out / "content_checks.json").write_text(json.dumps(deep_results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("[4/4] Reports written:", out.resolve())
    print(f"Dates: {len(dates)} | " + " | ".join(f"{k}={statuses[k]}" for k in ("PASS", "FAIL", "UNVERIFIED_NO_OBJECTS", "UNVERIFIED_PUBLICATION")))
    print("Issues by category:", dict(kinds))
    print("Note: dates without R2 objects require CMR comparison before calling them missing.")
    return 2 if issues else 0


def main():
    args = arguments()
    try:
        return audit(args)
    except (ValueError, ClientError, BotoCoreError, OSError, RuntimeError) as exc:
        # Never print credentials or constructed endpoints.
        if isinstance(exc, ClientError):
            detail = exc.response.get("Error", {})
            print(f"ERROR: R2 API call failed: {detail.get('Code', 'unknown')}", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
