#!/usr/bin/env python3
"""Read and verify existing VIIRS Gold without rebuilding or reading Silver.

Validates a complete inventory before network access. Gold bytes are cached by
their verified SHA-256 under ignored data/platform; each cache use rechecks size
and complete SHA-256. Cached PASS rows additionally require identical source,
expectation, and verifier-code fingerprints.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from botocore.exceptions import BotoCoreError

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/2026-11-26-data-handoff"
DEFAULT_CACHE = ROOT / "data/platform/handoff-gold"
COUNTS = {"pixel_count": "pixel_count", "finite_temperature_count": "finite_temperature",
          "flagged_count": "flagged", "valid_count": "valid_flag_zero_temperature"}
SCHEMA = pa.schema([
    ("date", pa.date32()), ("boundary_version", pa.string()), ("dong_code", pa.string()),
    ("swath_id", pa.string()), ("swath_start_utc", pa.timestamp("us", tz="UTC")),
    ("first_scan_start_time_tai93_s", pa.float64()), ("last_scan_start_time_tai93_s", pa.float64()),
    ("pixel_count", pa.int64()), ("finite_temperature_count", pa.int64()),
    ("flagged_count", pa.int64()), ("valid_count", pa.int64()),
    ("temperature_mean_k", pa.float64()), ("temperature_min_k", pa.float64()),
    ("temperature_max_k", pa.float64()), ("valid_fraction", pa.float64()),
])


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def utc_datetime(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("Scene time_start_utc must explicitly specify UTC")
    return result.astimezone(timezone.utc)


def validate_inventory(objects, day_rows, scene_rows):
    """Fail before credentials/client/network work if any expectation is incomplete."""
    def required(row, fields, label):
        missing = [field for field in fields if not isinstance(row.get(field), str) or not row[field].strip()]
        if missing:
            raise ValueError(f"{label}: missing required fields {', '.join(sorted(missing))}")

    def number(row, field, label, positive=False):
        value = row.get(field, "")
        if not value.isdigit() or (positive and int(value) <= 0):
            raise ValueError(f"{label}: invalid {field}")

    def hexadecimal(value, label):
        if not re.fullmatch(r"[0-9a-f]{64}", value or ""):
            raise ValueError(f"{label}: expected lowercase SHA-256 with 64 hexadecimal characters")

    by_key = {}
    for row in objects:
        required(row, ("object_key", "tier", "date_kst", "size_bytes", "etag", "sha256"), "objects.csv")
        key = row["object_key"]
        if key in by_key:
            raise ValueError(f"Duplicate object_key: {key}")
        by_key[key] = row
        number(row, "size_bytes", key, positive=True)
        hexadecimal(row["sha256"], key)
        if any(ord(char) < 32 for char in row["etag"]):
            raise ValueError(f"{key}: invalid ETag control character")
        date.fromisoformat(row["date_kst"])
    days, seen_days = {}, set()
    gold_objects = [row for row in objects if row["tier"] == "gold"]
    gold_dates = {row["date_kst"] for row in gold_objects}
    day_fields = ("date_kst", "boundary_version", "pipeline_version", "input_fingerprint",
                  "processing_code_sha256", "boundary_sha256", "silver_key", "gold_key",
                  "gold_rows_recorded_server", *COUNTS.values())
    for row in day_rows:
        required(row, ("date_kst",), "days.csv")
        day = row["date_kst"]
        if day in seen_days:
            raise ValueError(f"Duplicate day expectation: {day}")
        seen_days.add(day)
        date.fromisoformat(day)
        if day not in gold_dates:
            if row.get("server_status") != "NO_PAIRED_CMR":
                raise ValueError(f"{day}: no Gold object and no explicit NO_PAIRED_CMR status")
            continue
        required(row, day_fields, day)
        for field in ("input_fingerprint", "processing_code_sha256", "boundary_sha256"):
            hexadecimal(row[field], day + ":" + field)
        for field in ("gold_rows_recorded_server", *COUNTS.values()):
            number(row, field, day)
        silver = by_key.get(row["silver_key"], {})
        gold_obj = by_key.get(row["gold_key"], {})
        for tier, obj in (("silver", silver), ("gold", gold_obj)):
            if (obj.get("tier") != tier or obj.get("date_kst") != day
                    or obj.get("boundary_version") != row["boundary_version"]):
                raise ValueError(f"{day}: missing or mismatched {tier} object expectation")
        days[day] = {**row, "source_silver_sha256": silver["sha256"]}
    if not gold_objects or len(gold_objects) != len(days) or {r["date_kst"] for r in gold_objects} != set(days):
        raise ValueError("Gold objects and daily expectations must have a nonempty one-to-one correspondence")
    scenes, seen = defaultdict(list), set()
    for row in scene_rows:
        required(row, ("date_kst", "swath_id", "time_start_utc", *COUNTS.values()), "scenes.csv")
        key = (row["date_kst"], row["swath_id"])
        if key in seen or row["date_kst"] not in days:
            raise ValueError(f"Duplicate or unexpected scene expectation: {key}")
        seen.add(key)
        utc_datetime(row["time_start_utc"])
        for field in COUNTS.values():
            number(row, field, str(key))
        scenes[row["date_kst"]].append(row)
    for day, expected in days.items():
        if not scenes[day]:
            raise ValueError(f"{day}: no scene expectations")
        for field in COUNTS.values():
            if sum(int(row[field]) for row in scenes[day]) != int(expected[field]):
                raise ValueError(f"{day}: scene expectations do not sum to daily {field}")
    return gold_objects, days, scenes


def load_inventory(directory):
    rows, hashes = {}, {}
    for name in ("objects.csv", "days.csv", "scenes.csv"):
        content = (Path(directory) / name).read_bytes()
        hashes[name] = hashlib.sha256(content).hexdigest()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        rows[name] = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows[name]):
            raise ValueError(f"{name}: malformed CSV row width")
    objects, days, scenes = validate_inventory(rows["objects.csv"], rows["days.csv"], rows["scenes.csv"])
    return objects, days, scenes, hashes


def verified_gold_path(obj, cache_dir, fetch_response):
    """Return verified local bytes; a corrupt/stale cache is never parsed as Gold."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    expected_size = int(obj["size_bytes"])
    expected_sha = obj["sha256"]
    path = cache_dir / (expected_sha + ".parquet")
    if path.is_file() and path.stat().st_size == expected_size and digest(path) == expected_sha:
        return path, True
    for attempt in range(3):
        try:
            with TemporaryDirectory(dir=cache_dir, prefix=".download-") as tmp:
                staged = Path(tmp) / "gold.parquet"
                response = fetch_response(obj)
                h, size = hashlib.sha256(), 0
                try:
                    with staged.open("wb") as stream:
                        for part in response["Body"].iter_chunks(1024 * 1024):
                            if part:
                                stream.write(part)
                                h.update(part)
                                size += len(part)
                finally:
                    response["Body"].close()
                if size != expected_size or h.hexdigest() != expected_sha:
                    raise ValueError("Downloaded Gold size or complete SHA-256 mismatch")
                os.replace(staged, path)
            break
        except BotoCoreError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return path, False


def check_table(table, expected, scenes):
    errors = []
    if not table.schema.equals(SCHEMA, check_metadata=False):
        raise ValueError("Gold schema differs from the declared 15-column contract")
    required = [f.name for f in SCHEMA if not f.name.startswith("temperature_")]
    if any(table[name].null_count for name in required):
        raise ValueError("Null in a required Gold identity/count/time/fraction field")
    metadata = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    for name, source in (("pipeline_version", "pipeline_version"), ("boundary_version", "boundary_version"),
                         ("date_kst", "date_kst"), ("input_fingerprint", "input_fingerprint"),
                         ("processing_code_sha256", "processing_code_sha256"), ("boundary_sha256", "boundary_sha256"),
                         ("source_silver_sha256", "source_silver_sha256"), ("source_silver", "silver_key")):
        if metadata.get(name) != expected[source]:
            errors.append(f"metadata_{name}")
    if metadata.get("units") != "Kelvin; TAI93 seconds":
        errors.append("metadata_units")
    if table.num_rows != int(expected["gold_rows_recorded_server"]):
        errors.append("server_row_count")
    if table.num_rows and set(table["date"].unique().to_pylist()) != {date.fromisoformat(expected["date_kst"])}:
        errors.append("date_kst")
    if table.num_rows and set(table["boundary_version"].unique().to_pylist()) != {expected["boundary_version"]}:
        errors.append("boundary_version")
    codes, swaths = table["dong_code"].to_pylist(), table["swath_id"].to_pylist()
    if any(not code.isascii() or not code.isdigit() or len(code) != 8 for code in codes):
        errors.append("dong_code_format")
    if len(set(zip(codes, swaths))) != table.num_rows:
        errors.append("duplicate_dong_swath_key")
    arrays = {name: table[name].to_numpy(zero_copy_only=False) for name in COUNTS}
    pixels, finite, flagged, valid = (arrays[x] for x in COUNTS)
    if any(np.any(x < 0) for x in arrays.values()) or np.any(pixels <= 0):
        errors.append("nonpositive_pixel_or_negative_count")
    if (np.any(finite > pixels) or np.any(flagged > pixels) or np.any(valid > finite)
            or np.any(valid > pixels - flagged) or np.any(valid < finite - flagged)):
        errors.append("count_inequalities")
    totals = {name: int(values.sum()) for name, values in arrays.items()}
    for name, column in COUNTS.items():
        if totals[name] != int(expected[column]):
            errors.append(f"quality_total_{name}")
    fractions = table["valid_fraction"].to_numpy(zero_copy_only=False)
    if (not np.isfinite(fractions).all() or np.any(fractions < 0) or np.any(fractions > 1)
            or not np.allclose(fractions, valid / pixels, rtol=1e-9, atol=1e-12)):
        errors.append("valid_fraction_denominator")
    temperatures = {}
    for name in ("temperature_mean_k", "temperature_min_k", "temperature_max_k"):
        null = pc.is_null(table[name]).to_numpy(zero_copy_only=False)
        if not np.array_equal(null, valid == 0):
            errors.append(f"null_rule_{name}")
        temperatures[name] = table[name].to_numpy(zero_copy_only=False)
        if not np.isfinite(temperatures[name][valid > 0]).all():
            errors.append(f"nonfinite_{name}")
    low, mean, high = (temperatures[x][valid > 0] for x in
                       ("temperature_min_k", "temperature_mean_k", "temperature_max_k"))
    if np.any(low <= 0) or np.any(mean <= 0) or np.any(high <= 0):
        errors.append("nonpositive_kelvin")
    if np.any(low > mean + 1e-8) or np.any(mean > high + 1e-8):
        errors.append("min_mean_max_order")
    first = table["first_scan_start_time_tai93_s"].to_numpy(zero_copy_only=False)
    last = table["last_scan_start_time_tai93_s"].to_numpy(zero_copy_only=False)
    if not np.isfinite(first).all() or not np.isfinite(last).all() or np.any(first <= 0) or np.any(first > last):
        errors.append("scan_time_order_or_units")
    by_scene = {r["swath_id"]: r for r in scenes}
    if not set(swaths).issubset(by_scene):
        errors.append("unexpected_swath")
    grouped = table.group_by("swath_id").aggregate(
        [(name, "sum") for name in COUNTS] + [("swath_start_utc", "min"), ("swath_start_utc", "max")]).to_pylist()
    actual = {r["swath_id"]: r for r in grouped}
    for swath, source in by_scene.items():
        values = actual.get(swath, {})
        if values:
            expected_time = utc_datetime(source["time_start_utc"])
            if values["swath_start_utc_min"] != expected_time or values["swath_start_utc_max"] != expected_time:
                errors.append(f"swath_start_utc_{swath}")
        for name, source_col in COUNTS.items():
            if int(values.get(name + "_sum", 0)) != int(source[source_col]):
                errors.append(f"scene_sum_{swath}_{name}")
    return {"errors": sorted(set(errors)), "row_count": table.num_rows, **totals,
            "groups_without_valid_temperature": int(np.count_nonzero(valid == 0)),
            "temperature_min_k": float(low.min()) if low.size else "",
            "temperature_max_k": float(high.max()) if high.size else "",
            "distinct_swaths_with_land_pixels": len(set(swaths))}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", type=Path, default=REPORT / "viirs")
    p.add_argument("--out", type=Path, default=REPORT / "gold")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                   help="persistent SHA-256-addressed Gold byte cache (default: ignored data/platform/handoff-gold)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--reuse", action="store_true", help="reuse exact source/expectation/code-matched PASS rows")
    args = p.parse_args(argv)
    if not 1 <= args.workers <= 8:
        p.error("workers must be 1..8")
    try:
        objects, days, scenes, input_hashes = load_inventory(args.inventory)
    except (ValueError, KeyError, OSError) as exc:
        p.error(f"Invalid inventory; no network reads performed: {exc}")
    args.out.mkdir(parents=True, exist_ok=True)
    # A full cache recheck needs no credentials. Client creation is deferred until
    # the first missing/invalid byte cache, after every inventory row is checked.
    from threading import Lock
    client_lock = Lock()
    remote = {}

    def fetch_response(obj):
        with client_lock:
            if not remote:
                cfg = {}
                from dotenv import dotenv_values
                for layer in (dotenv_values(ROOT / ".env"), dotenv_values(ROOT / "platform/.env"), os.environ):
                    cfg.update({k: v for k, v in layer.items() if v and str(v).strip()})
                import boto3
                from botocore.config import Config
                remote["client"] = boto3.client(
                    "s3", endpoint_url=f'https://{cfg["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
                    aws_access_key_id=cfg["R2_ACCESS_KEY_ID"], aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
                    region_name="auto", config=Config(connect_timeout=15, read_timeout=60,
                    retries={"mode": "standard", "max_attempts": 3}, max_pool_connections=args.workers + 2,
                    request_checksum_calculation="when_required", response_checksum_validation="when_required"))
                remote["bucket"] = cfg["R2_BUCKET"]
        return remote["client"].get_object(Bucket=remote["bucket"], Key=obj["object_key"], IfMatch=obj["etag"])
    code_sha = digest(__file__)
    previous = {}
    output_csv = args.out / "gold_validation.csv"
    if args.reuse and output_csv.is_file():
        previous = {r["object_key"]: r for r in read_csv(output_csv)}

    def verify(obj):
        day = obj["date_kst"]
        expectation = {"day": days[day], "scenes": scenes[day], "code_sha256": code_sha}
        fingerprint = hashlib.sha256(json.dumps(expectation, sort_keys=True).encode()).hexdigest()
        base = {"date_kst": day, "object_key": obj["object_key"], "size_bytes": int(obj["size_bytes"]),
                "etag": obj["etag"], "sha256": obj["sha256"], "expectation_sha256": fingerprint,
                "status": "FAIL", "errors": ""}
        old = previous.get(obj["object_key"], {})
        if (old.get("status") == "PASS" and old.get("expectation_sha256") == fingerprint
                and old.get("etag") == obj["etag"] and old.get("sha256") == obj["sha256"]
                and old.get("size_bytes") == obj["size_bytes"]):
            return old, True, False
        try:
            path, cached_bytes = verified_gold_path(obj, args.cache_dir, fetch_response)
            checks = check_table(pq.read_table(path), days[day], scenes[day])
            base.update(checks)
            base["errors"] = ";".join(checks["errors"])
            base["status"] = "FAIL" if checks["errors"] else "PASS"
        except Exception as exc:
            base["errors"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return base, False, False
        return base, False, cached_bytes

    results, reused, reused_bytes = [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(verify, obj) for obj in objects]
        for future in as_completed(futures):
            row, cached, cached_bytes = future.result()
            results.append(row); reused += int(cached); reused_bytes += int(cached_bytes)
            if len(results) % 100 == 0:
                print(f"Gold verified {len(results)}/{len(objects)}", flush=True)
    fields = ["date_kst", "object_key", "size_bytes", "etag", "sha256", "expectation_sha256", "status", "errors",
              "row_count", *COUNTS, "groups_without_valid_temperature", "temperature_min_k", "temperature_max_k",
              "distinct_swaths_with_land_pixels"]
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n"); w.writeheader()
        w.writerows(sorted(results, key=lambda r: r["date_kst"]))
    passed = [r for r in results if r["status"] == "PASS"]
    summary = {"status": "PASS" if len(passed) == len(objects) and objects else "FAIL",
               "files_checked": len(results), "status_counts": dict(Counter(r["status"] for r in results)),
               "reused_exact_checks": reused, "total_gold_bytes": sum(int(r["size_bytes"]) for r in results),
               "reused_verified_byte_cache": reused_bytes,
               "byte_cache_policy": "content-addressed SHA-256; full hash and size checked on each use; no stale PASS reuse",
               "row_count": sum(int(r["row_count"]) for r in passed),
               "totals": {name: sum(int(r[name]) for r in passed) for name in COUNTS},
               "groups_without_valid_temperature": sum(int(r["groups_without_valid_temperature"]) for r in passed),
               "temperature_min_k": min((float(r["temperature_min_k"]) for r in passed if r["temperature_min_k"] != ""), default=None),
               "temperature_max_k": max((float(r["temperature_max_k"]) for r in passed if r["temperature_max_k"] != ""), default=None),
               "cloud_qc_status": "unverified", "code_sha256": code_sha,
               "input_inventory_sha256": input_hashes["objects.csv"],
               "input_days_sha256": input_hashes["days.csv"],
               "input_scenes_sha256": input_hashes["scenes.csv"],
               "checks": ["full_byte_SHA256_and_size", "schema_units_lineage", "unique_dong_swath_key",
                          "inventory_preflight_before_network", "processing_boundary_and_source_silver_hashes",
                          "scene_UTC_start_matches_source_expectation",
                          "required_fields_not_null", "count_inequalities", "daily_and_scene_quality_sums",
                          "null_statistics_when_no_valid_pixels", "finite_positive_Kelvin", "min_mean_max_order",
                          "valid_fraction_denominator", "scan_time_finiteness_and_order"],
               "range_interpretation": "Observed positive-Kelvin range and arithmetic consistency only; no arbitrary climate cutoff or outlier deletion applied",
               "not_performed": ["Silver_content_read", "Bronze_content_read", "cloud_shadow_QC", "scientific_temperature_bias_validation", "reprocessing"]}
    (args.out / "gold_validation_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
