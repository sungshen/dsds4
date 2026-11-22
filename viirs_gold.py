"""Legacy per-swath Gold aggregation; daily rebuild owns dong-observations.parquet.

Examples:
    python viirs_gold.py --r2 --env-file platform/.env --dry-run
    python viirs_gold.py --r2 --start 2024-01-01 --stop 2024-02-01
"""

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from viirs_pipeline import KST, configured_value, timestamp
from viirs_s3 import checksum, fetch_file, object_metadata, put_file, storage_client

PROCESSING_VERSION = "viirs-gold-i05-1.0"
VALID_POLICY = "finite I5 brightness temperature and i05_quality_flags == 0"
SOURCE_KEY = re.compile(
    r"(?P<prefix>.+)/silver/(?P<version>[A-Za-z0-9][A-Za-z0-9_-]*)/"
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<swath>A\d{7}\.\d{4})\.parquet"
)
SOURCE_COLUMNS = ["dong_code", "i05_brightness_temperature_k", "i05_quality_flags"]
GOLD_SCHEMA = pa.schema([
    ("date", pa.date32()),
    ("dong_code", pa.string()),
    ("boundary_version", pa.string()),
    ("source_observation_id", pa.string()),
    ("observation_time_utc", pa.timestamp("us", tz="UTC")),
    ("pixel_count", pa.int64()),
    ("nonnull_count", pa.int64()),
    ("valid_count", pa.int64()),
    ("flagged_count", pa.int64()),
    ("temperature_mean_k", pa.float64()),
    ("temperature_min_k", pa.float64()),
    ("temperature_max_k", pa.float64()),
    ("valid_fraction", pa.float64()),
])


def parse_source_key(key):
    match = SOURCE_KEY.fullmatch(key)
    if match is None:
        raise ValueError(f"Unexpected Silver key: {key}")
    location = match.groupdict()
    day = date(int(location["year"]), int(location["month"]), int(location["day"]))
    if timestamp(location["swath"]).astimezone(KST).date() != day:
        raise ValueError(f"Silver path date differs from the swath's KST date: {key}")
    return location, day


def aggregate_gold(source_path, destination, *, source_key, source_sha256,
                   quality_report=None, batch_size=65536):
    """Verify and stream one local Silver file into a typed Gold Parquet."""
    source_path, destination = Path(source_path), Path(destination)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if destination.exists():
        raise ValueError(f"Local Gold output already exists: {destination}")
    if checksum(source_path)[0] != source_sha256:
        raise ValueError("Silver SHA-256 differs from the verified source digest")
    location, day = parse_source_key(source_key)
    observed = timestamp(location["swath"])
    groups = {}
    total_pixels = 0
    with pq.ParquetFile(source_path) as parquet:
        metadata = {key.decode(): value.decode() for key, value in (parquet.schema_arrow.metadata or {}).items()}
        required = {"swath_start_utc", "boundary_version", "sources"}
        if not required.issubset(metadata):
            raise ValueError("Silver requires observation time, boundary version and source filenames")
        actual_observed = datetime.fromisoformat(metadata["swath_start_utc"].replace("Z", "+00:00"))
        if actual_observed.tzinfo is None or actual_observed != observed:
            raise ValueError("Silver metadata observation time differs from its object key")
        if metadata["boundary_version"] != location["version"]:
            raise ValueError("Silver metadata boundary version differs from its object key")
        sources = json.loads(metadata["sources"])
        if not isinstance(sources, list) or not sources or not all(isinstance(name, str) for name in sources):
            raise ValueError("Silver source filenames must be a nonempty JSON string list")
        if not set(SOURCE_COLUMNS).issubset(parquet.schema_arrow.names):
            raise ValueError("Silver does not contain the required VIIRS I5 pixel columns")
        if not pa.types.is_string(parquet.schema_arrow.field("dong_code").type):
            raise ValueError("dong_code must preserve string administrative codes")
        if not pa.types.is_integer(parquet.schema_arrow.field("i05_quality_flags").type):
            raise ValueError("i05_quality_flags must be an integer bit field")
        for batch in parquet.iter_batches(columns=SOURCE_COLUMNS, batch_size=batch_size):
            codes = batch.column(0).to_pylist()
            temperatures = batch.column(1).to_numpy(zero_copy_only=False)
            flags = batch.column(2).to_numpy(zero_copy_only=False)
            if batch.column(0).null_count or batch.column(2).null_count:
                raise ValueError("Silver dong codes and quality flags must be present for every pixel")
            for code, value, flag in zip(codes, temperatures, flags):
                if not code or not code.isascii() or not code.isdigit():
                    raise ValueError("Silver contains an invalid administrative code")
                if not 0 <= flag <= 65535:
                    raise ValueError("Silver quality flag is outside the uint16 range")
                # Counts, finite values, valid values, flagged values, sum, min, max.
                group = groups.setdefault(code, [0, 0, 0, 0, 0.0, None, None])
                group[0] += 1
                group[3] += int(flag != 0)
                if np.isfinite(value):
                    group[1] += 1
                    if flag == 0:
                        value = float(value)
                        group[2] += 1
                        group[4] += value
                        group[5] = value if group[5] is None else min(group[5], value)
                        group[6] = value if group[6] is None else max(group[6], value)
            total_pixels += batch.num_rows

    if quality_report is not None:
        if not isinstance(quality_report, dict) or quality_report.get("pixel_count") != total_pixels:
            raise ValueError("Silver quality report pixel_count differs from the source file")
    records = []
    for code, (count, nonnull, valid, flagged, total, minimum, maximum) in sorted(groups.items()):
        records.append({
            "date": day, "dong_code": code, "boundary_version": location["version"],
            "source_observation_id": location["swath"], "observation_time_utc": observed,
            "pixel_count": count, "nonnull_count": nonnull, "valid_count": valid,
            "flagged_count": flagged, "temperature_mean_k": total / valid if valid else None,
            "temperature_min_k": minimum, "temperature_max_k": maximum,
            "valid_fraction": valid / count,
        })
    gold_metadata = {
        "processing_version": PROCESSING_VERSION,
        "processing_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_object_key": source_key, "source_sha256": source_sha256,
        "source_sha256_encoding": "base64", "sources": metadata["sources"],
        "swath_start_utc": observed.isoformat(), "boundary_version": location["version"],
        "units": "K", "valid_policy": VALID_POLICY,
        "valid_fraction_denominator": "all Silver pixels assigned to this dong in this observation",
        "cloud_qc_status": "unverified", "resampling": "none",
        "quality_report_status": "available" if quality_report is not None else "missing",
        "quality_report": json.dumps(quality_report, sort_keys=True),
        "gold_spatial_scope": "only dong codes present in the Silver pixels",
    }
    schema = GOLD_SCHEMA.with_metadata(gold_metadata)
    table = pa.Table.from_pylist(records, schema=schema)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent, prefix="gold-write-") as temporary:
        staged = Path(temporary) / "gold.parquet"
        pq.write_table(table, staged, compression="zstd")
        if not pq.read_schema(staged).equals(schema, check_metadata=True):
            raise ValueError("Gold Parquet schema verification failed")
        if pq.ParquetFile(staged).metadata.num_rows != len(records):
            raise ValueError("Gold Parquet row count verification failed")
        os.replace(staged, destination)
    return len(records)


def source_head(client, bucket, key, location):
    raw = client.head_object(Bucket=bucket, Key=key)
    metadata = {name: value for name, value in raw.get("Metadata", {}).items() if name != "sha256"}
    required = {"swath", "i05-granule", "geolocation-granule", "boundary-sha256"}
    if set(metadata) != required or metadata["swath"] != location["swath"]:
        raise ValueError(f"Silver object metadata is incomplete or inconsistent: {key}")
    if not re.fullmatch(r"[a-f0-9]{64}", metadata["boundary-sha256"]):
        raise ValueError(f"Silver boundary checksum is invalid: {key}")
    return object_metadata(client, bucket, key, metadata, r2=True)


def gold_object_metadata(source_key, source_obj, location):
    return {
        "source-key-sha256": hashlib.sha256(source_key.encode()).hexdigest(),
        "source-sha256": source_obj["verified_sha256"],
        "processing-version": PROCESSING_VERSION,
        "processing-code-sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "boundary-version": location["version"],
        "source-swath": location["swath"],
    }


def process_scene(client, bucket, source_key, stage_dir=None):
    location, _ = parse_source_key(source_key)
    source_obj = source_head(client, bucket, source_key, location)
    if source_obj is None:
        raise ValueError(f"Silver source disappeared: {source_key}")
    gold_key = source_key.replace("/silver/", "/gold/", 1)
    metadata = gold_object_metadata(source_key, source_obj, location)
    existing = object_metadata(client, bucket, gold_key, metadata, r2=True)
    if existing is not None:
        return {"status": "verified_existing", "gold_key": gold_key}
    report_key = source_key.replace("/silver/", "/quality/", 1).removesuffix(".parquet") + ".json"
    expected_report = {name: value for name, value in source_obj["Metadata"].items() if name != "sha256"}
    expected_report["report-version"] = "1"
    report_obj = object_metadata(client, bucket, report_key, expected_report, r2=True)
    with TemporaryDirectory(dir=stage_dir, prefix="viirs-gold-") as directory:
        directory = Path(directory)
        silver_path, gold_path = directory / "silver.parquet", directory / "gold.parquet"
        fetch_file(client, bucket, source_key, silver_path, source_obj, r2=True)
        report = None
        if report_obj is not None:
            if report_obj["ContentLength"] > 1024 * 1024:
                raise ValueError(f"Quality report exceeds the 1 MiB JSON limit: {report_key}")
            report_path = directory / "quality.json"
            fetch_file(client, bucket, report_key, report_path, report_obj, r2=True)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = aggregate_gold(silver_path, gold_path, source_key=source_key,
                              source_sha256=source_obj["verified_sha256"], quality_report=report)
        put_file(client, bucket, gold_key, gold_path, metadata, "application/vnd.apache.parquet", r2=True)
    return {"status": "created", "gold_key": gold_key, "row_count": rows}


def inventory(client, bucket, prefix, start, stop):
    items = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/silver/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            # Current daily Silver/Gold is produced together by viirs_full_rebuild.py.
            # Never feed multiple observations into the legacy single-swath aggregator.
            if obj["Key"].rsplit("/", 1)[-1] == "all-observations.parquet":
                continue
            _, day = parse_source_key(obj["Key"])
            if start <= day < stop:
                items.append(obj["Key"])
    return sorted(items)


def load_env_file(path):
    """Apply only R2 values, with process environment taking precedence."""
    if path is None:
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r2", action="store_true", required=True, help="Use Cloudflare R2")
    parser.add_argument("--env-file", type=Path, help="Optional local .env file with R2 credentials")
    parser.add_argument("--bucket", help="Override R2_BUCKET")
    parser.add_argument("--prefix", default="viirs-daily")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1), help="Inclusive KST date")
    parser.add_argument("--stop", type=date.fromisoformat, default=date(2026, 1, 1), help="Exclusive KST date")
    parser.add_argument("--limit", type=int, help="Maximum number of selected source observations")
    parser.add_argument("--stage-dir", type=Path, help="Existing directory for one-scene temporary files")
    parser.add_argument("--dry-run", action="store_true", help="List planned objects without downloading or uploading")
    args = parser.parse_args()
    if args.env_file is not None and not args.env_file.is_file():
        parser.error("--env-file must be an existing file")
    if args.stage_dir is not None and not args.stage_dir.is_dir():
        parser.error("--stage-dir must be an existing directory")
    args.prefix = args.prefix.strip("/")
    if not args.prefix or any(part in ("", ".", "..") for part in args.prefix.split("/")):
        parser.error("Use a nonempty normalized object prefix")
    if args.start >= args.stop or (args.limit is not None and args.limit < 1):
        parser.error("Use a nonempty date range and a positive --limit")
    load_env_file(args.env_file)
    account = configured_value("R2_ACCOUNT_ID")
    bucket = args.bucket or configured_value("R2_BUCKET")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account or "") or not bucket:
        parser.error("Configure R2_ACCOUNT_ID and R2_BUCKET")
    client = storage_client(SimpleNamespace(r2_account_id=account, profile=None, region=None))
    keys = inventory(client, bucket, args.prefix, args.start, args.stop)
    if args.limit is not None:
        keys = keys[:args.limit]
    print(f"Selected {len(keys)} Silver observations; dates use KST", flush=True)
    for index, key in enumerate(keys, start=1):
        if args.dry_run:
            print(f"PLAN {key} -> {key.replace('/silver/', '/gold/', 1)}", flush=True)
        else:
            result = process_scene(client, bucket, key, args.stage_dir)
            print(f"{index}/{len(keys)} {result['status']} {result['gold_key']}", flush=True)
    print("Dry run complete; no objects written" if args.dry_run else "Gold processing complete", flush=True)


if __name__ == "__main__":
    main()
