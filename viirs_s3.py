"""Ingest NASA VIIRS into AWS S3 or Cloudflare R2, staging one swath pair at a time."""

import argparse
import base64
import hashlib
import json
import re
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from viirs_pipeline import (
    BRONZE, KST, PRODUCTS, boundaries, configured_value, download, earthdata_token,
    search, timestamp, write_crop,
)


def checksum(path, *, with_md5=False):
    digest = hashlib.sha256()
    content_md5 = hashlib.md5(usedforsecurity=False) if with_md5 else None
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if content_md5 is not None:
                content_md5.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii"), content_md5


def boundary_checksum(dongs):
    digest = hashlib.sha256()
    for code, geometry in dongs.geometry.items():
        digest.update(f"{code}:".encode("ascii"))
        digest.update(geometry.normalize().wkb)
    return digest.hexdigest()


def object_metadata(client, bucket, key, expected, *, r2=False):
    options = {} if r2 else {"ChecksumMode": "ENABLED"}
    try:
        obj = client.head_object(Bucket=bucket, Key=key, **options)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return None
        raise
    metadata = obj.get("Metadata", {}).copy()
    sha256 = metadata.pop("sha256", "") if r2 else obj.get("ChecksumSHA256", "")
    valid_etag = not r2 or re.fullmatch(r"[0-9a-f]{32}", obj.get("ETag", "").strip('"'))
    if (
        obj["ContentLength"] <= 0
        or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", sha256)
        or not valid_etag
        or metadata != expected
    ):
        raise ValueError(f"Existing object has unexpected content metadata: s3://{bucket}/{key}")
    obj["verified_sha256"] = sha256
    return obj


def put_file(client, bucket, key, path, metadata, content_type, *, r2=False):
    size = path.stat().st_size
    if not 0 < size <= 5 * 1024**3:
        raise ValueError("Conditional single-PUT objects must be nonempty and at most 5 GiB")
    sha256, content_md5 = checksum(path, with_md5=r2)
    options = {"ChecksumSHA256": sha256}
    stored_metadata = metadata
    if r2:
        # R2 supports Content-MD5 on a single PUT and SHA-256 only as a composite checksum.
        options = {
            "ContentMD5": base64.b64encode(content_md5.digest()).decode("ascii"),
            "StorageClass": "STANDARD",
        }
        stored_metadata = {**metadata, "sha256": sha256}
    try:
        with path.open("rb") as stream:
            client.put_object(
                Bucket=bucket, Key=key, Body=stream, ContentLength=size,
                IfNoneMatch="*", Metadata=stored_metadata, ContentType=content_type, **options,
            )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in (409, 412):
            raise
    obj = object_metadata(client, bucket, key, metadata, r2=r2)
    if (obj is None or obj["ContentLength"] != size or obj["verified_sha256"] != sha256
            or (r2 and obj["ETag"].strip('"') != content_md5.hexdigest())):
        raise ValueError(f"Uploaded object did not match the local file: s3://{bucket}/{key}")


def fetch_file(client, bucket, key, path, obj, *, r2=False):
    options = {} if r2 else {"ChecksumMode": "ENABLED"}
    response = client.get_object(
        Bucket=bucket, Key=key, IfMatch=obj["ETag"], **options,
    )
    with closing(response["Body"]) as body, path.open("wb") as stream:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            stream.write(chunk)
    if path.stat().st_size != obj["ContentLength"] or checksum(path)[0] != obj["verified_sha256"]:
        raise ValueError(f"Downloaded object failed verification: s3://{bucket}/{key}")


def crop_quality(path, total_dongs):
    import numpy as np
    import pyarrow.parquet as pq

    rows = missing = bowtie = flagged = valid = 0
    valid_dongs = set()
    columns = ["dong_code", "i05_brightness_temperature_k", "i05_quality_flags"]
    with pq.ParquetFile(path) as parquet:
        for batch in parquet.iter_batches(columns=columns, batch_size=65536):
            temperature = batch.column(1).to_numpy(zero_copy_only=False)
            absent = ~np.isfinite(temperature)
            flags = batch.column(2).to_numpy(zero_copy_only=False)
            rows += batch.num_rows
            missing += int(absent.sum())
            flagged += int((flags != 0).sum())
            valid += int((~absent & (flags == 0)).sum())
            bowtie += int((absent & ((flags & 256) != 0)).sum())
            valid_dongs.update(batch.column(0).filter(~absent).unique().to_pylist())
    return {
        "pixel_count": rows,
        "missing_brightness_temperature": missing,
        "missing_bowtie_deleted": bowtie,
        "missing_other": missing - bowtie,
        "dong_count": total_dongs,
        "dongs_without_valid_temperature": total_dongs - len(valid_dongs),
        "finite_temperature_count": rows - missing,
        "flagged_count": flagged,
        "valid_count": valid,
        "valid_fraction": valid / rows if rows else None,
        "valid_policy": "finite I5 brightness temperature and i05_quality_flags == 0",
        "valid_fraction_denominator": "all Silver pixels in this observation",
        "dongs_without_valid_temperature_policy": "no finite I5 brightness temperature; flags not filtered",
        "cloud_qc_status": "unverified",
    }


def ingest_swath(client, session, args, token, key, assets, dongs, boundary_hash):
    day = timestamp(key).astimezone(KST).strftime("%Y/%m/%d")
    bucket, prefix = args.bucket, args.prefix
    r2 = bool(args.r2_account_id)
    keys = {product: f"{prefix}/bronze/{day}/{assets[product][0]}" for product in PRODUCTS}
    metadata = {product: {"nasa-granule": assets[product][0]} for product in PRODUCTS}
    originals = {
        product: object_metadata(client, bucket, keys[product], metadata[product], r2=r2)
        for product in PRODUCTS
    }
    needs_crop = needs_quality = False
    if dongs is not None:
        crop_key = f"{prefix}/silver/{args.boundary_version}/{day}/{key}.parquet"
        crop_metadata = {
            "swath": key,
            "i05-granule": assets[PRODUCTS[0]][0],
            "geolocation-granule": assets[PRODUCTS[1]][0],
            "boundary-sha256": boundary_hash,
        }
        crop_object = object_metadata(client, bucket, crop_key, crop_metadata, r2=r2)
        needs_crop = crop_object is None
        quality_key = f"{prefix}/quality/{args.boundary_version}/{day}/{key}.json"
        quality_metadata = {**crop_metadata, "report-version": "1"}
        needs_quality = needs_crop or object_metadata(
            client, bucket, quality_key, quality_metadata, r2=r2
        ) is None
    if all(originals.values()) and not needs_quality:
        return

    with TemporaryDirectory(prefix="viirs-", dir=args.stage_dir) as directory:
        root = Path(directory)
        pair = {}
        for product in PRODUCTS:
            obj = originals[product]
            if obj is None:
                local = (BRONZE / day / assets[product][0]).is_file()
                if not local and not token:
                    raise SystemExit("Set EARTHDATA_TOKEN before downloading missing VIIRS originals.")
                path = download(session, BRONZE if local else root, key, assets[product], token)
                put_file(client, bucket, keys[product], path, metadata[product], "application/x-netcdf", r2=r2)
            elif needs_crop:
                path = root / assets[product][0]
                fetch_file(client, bucket, keys[product], path, obj, r2=r2)
            else:
                continue
            pair[product] = path
        if needs_crop:
            path = root / f"{key}.parquet"
            write_crop(path, key, pair, dongs, args.boundary, args.boundary_version)
        elif needs_quality:
            path = root / f"{key}.parquet"
            fetch_file(client, bucket, crop_key, path, crop_object, r2=r2)
        if needs_quality:
            report = crop_quality(path, len(dongs))
        if needs_crop:
            put_file(client, bucket, crop_key, path, crop_metadata, "application/vnd.apache.parquet", r2=r2)
        if needs_quality:
            report_path = root / f"{key}.json"
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            put_file(client, bucket, quality_key, report_path, quality_metadata, "application/json", r2=r2)


def storage_client(args):
    config = Config(connect_timeout=30, read_timeout=180, retries={"mode": "standard"})
    if args.r2_account_id:
        key_id = configured_value("R2_ACCESS_KEY_ID")
        secret_key = configured_value("R2_SECRET_ACCESS_KEY")
        if not key_id or not secret_key:
            raise SystemExit("Set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY before using R2.")
        r2_config = config.merge(Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
        ))
        return boto3.client(
            "s3", region_name="auto", config=r2_config,
            endpoint_url=f"https://{args.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=key_id, aws_secret_access_key=secret_key,
        )
    return boto3.Session(profile_name=args.profile).client("s3", region_name=args.region, config=config)


def daily_keys(keys):
    selected = {}
    for key in keys:
        observed = timestamp(key).astimezone(KST)
        day = observed.date()
        noon = datetime.combine(day, time(12), KST)
        candidate = (abs(observed - noon), key)
        if day not in selected or candidate < selected[day]:
            selected[day] = candidate
    return {day: candidate[1] for day, candidate in selected.items()}


def ingest(args):
    token = earthdata_token()
    dongs = boundaries(args.boundary, args.code_column) if args.boundary else None
    boundary_hash = boundary_checksum(dongs) if dongs is not None else None
    client = storage_client(args)
    storage = "R2" if args.r2_account_id else "S3"
    client.head_bucket(Bucket=args.bucket)
    with requests.Session() as session:
        retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        start = args.start
        while start < args.stop:
            stop = min(start + timedelta(days=30), args.stop)
            utc_start = datetime.combine(start, time.min, KST).astimezone(timezone.utc)
            utc_stop = datetime.combine(stop, time.min, KST).astimezone(timezone.utc)
            imagery, geo = (search(session, product, utc_start, utc_stop) for product in PRODUCTS)
            missing = imagery.keys() ^ geo.keys()
            if missing:
                raise RuntimeError(f"Unpaired VIIRS assets in CMR: {', '.join(sorted(missing))}")
            keys = sorted(imagery)
            if args.one_per_day:
                selected = daily_keys(keys)
                keys = [selected[day] for day in sorted(selected)]
                for offset in range((stop - start).days):
                    day = start + timedelta(days=offset)
                    if day not in selected:
                        print(f"{day} (KST): No paired VIIRS observations in CMR", flush=True)
                print(f"{start} to {stop} (KST): selected {len(keys)} daily pairs", flush=True)
            for completed, key in enumerate(keys, start=1):
                assets = {PRODUCTS[0]: imagery[key], PRODUCTS[1]: geo[key]}
                ingest_swath(client, session, args, token, key, assets, dongs, boundary_hash)
                if completed % 10 == 0 and completed < len(keys):
                    print(f"{start} to {stop} (KST): verified {completed}/{len(keys)} pairs in {storage}",
                          flush=True)
            print(f"{start} to {stop} (KST): verified {len(keys)} pairs in {storage}", flush=True)
            start = stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, choices=(2024, 2025), required=True)
    parser.add_argument("--one-per-day", action="store_true", help="Select the paired observation closest to noon KST")
    parser.add_argument("--bucket", help="Existing private bucket name, or R2_BUCKET with --r2")
    parser.add_argument("--r2", action="store_true", help="Use Cloudflare R2 with local R2 configuration")
    parser.add_argument("--r2-account-id", help="Use Cloudflare R2 and override R2_ACCOUNT_ID")
    parser.add_argument("--prefix", default="viirs", help="S3 object key prefix")
    parser.add_argument("--profile", help="AWS CLI profile, including IAM Identity Center profiles")
    parser.add_argument("--region", help="AWS region for the S3 bucket")
    parser.add_argument("--stage-dir", type=Path, help="Existing directory for temporary files")
    parser.add_argument("--start", type=date.fromisoformat, help="First KST date, inclusive")
    parser.add_argument("--stop", type=date.fromisoformat, help="Last KST date, exclusive")
    parser.add_argument("--boundary", type=Path, help="Nationwide boundary for the selected year")
    parser.add_argument("--code-column", help="Administrative code field in the boundary file")
    parser.add_argument("--boundary-version", help="Unique identifier for the boundary snapshot")
    args = parser.parse_args()
    first, last = date(args.year, 1, 1), date(args.year + 1, 1, 1)
    args.start = args.start or first
    args.stop = args.stop or last
    args.prefix = args.prefix.strip("/")
    if args.r2 or args.r2_account_id:
        args.r2_account_id = args.r2_account_id or configured_value("R2_ACCOUNT_ID")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", args.r2_account_id or ""):
            parser.error("Set R2_ACCOUNT_ID to a 32-character hexadecimal Cloudflare account ID.")
        args.bucket = args.bucket or configured_value("R2_BUCKET")
        if args.profile or args.region:
            parser.error("Cloudflare R2 uses dedicated R2 credentials and region auto.")
    if not args.bucket:
        parser.error("Provide --bucket or configure R2_BUCKET for Cloudflare R2.")
    if not first <= args.start < args.stop <= last:
        parser.error("Choose a nonempty date range within the selected KST year.")
    if not args.prefix:
        parser.error("Provide a nonempty S3 prefix.")
    crop_options = (args.boundary, args.code_column, args.boundary_version)
    if any(crop_options) and not all(crop_options):
        parser.error("Silver requires --boundary, --code-column and --boundary-version.")
    if args.boundary_version and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.boundary_version):
        parser.error("Use letters, digits, hyphens and underscores for the boundary version.")
    if args.stage_dir is not None and not args.stage_dir.is_dir():
        parser.error("--stage-dir must be an existing directory.")
    ingest(args)


if __name__ == "__main__":
    main()
