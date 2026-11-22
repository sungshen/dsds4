#!/usr/bin/env python3
"""2024-2025 VIIRS complete rebuild: all Korean-BBOX scene pairs -> Bronze -> Silver -> Quality -> Gold.

Run from /root/viirs with existing viirs_pipeline.py, viirs_s3.py, .env, boundaries/ and .venv.
Writes to the EXISTING R2 prefixes. Bronze is never overwritten. New date-level Silver,
Quality and Gold are uploaded and verified before obsolete per-swath files are deleted.
For each VIIRS pixel Silver retains swath identity and TAI93 scan START (not exposure time).
No rainfall label is inferred from brightness temperatures.
"""
from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "all-scenes-scan-time-v2.1"
SILVER_FILE = "all-observations.parquet"
QUALITY_FILE = "all-observations.json"
GOLD_FILE = "dong-observations.parquet"
REQUIRED = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
MAX_BRONZE_SINGLE_PUT = 5 * 1024**3
SILVER_SCHEMA = pa.schema([
    ("dong_code", pa.string()), ("row", pa.int32()), ("column", pa.int32()),
    ("longitude", pa.float64()), ("latitude", pa.float64()),
    ("i05_brightness_temperature_k", pa.float32()), ("i05_quality_flags", pa.uint16()),
    ("swath_id", pa.string()), ("swath_start_utc", pa.timestamp("us", tz="UTC")),
    ("scan_index", pa.int32()), ("scan_start_time_tai93_s", pa.float64()),
])
GOLD_SCHEMA = pa.schema([
    ("date", pa.date32()), ("boundary_version", pa.string()), ("dong_code", pa.string()),
    ("swath_id", pa.string()), ("swath_start_utc", pa.timestamp("us", tz="UTC")),
    ("first_scan_start_time_tai93_s", pa.float64()),
    ("last_scan_start_time_tai93_s", pa.float64()),
    ("pixel_count", pa.int64()), ("finite_temperature_count", pa.int64()),
    ("flagged_count", pa.int64()), ("valid_count", pa.int64()),
    ("temperature_mean_k", pa.float64()), ("temperature_min_k", pa.float64()),
    ("temperature_max_k", pa.float64()), ("valid_fraction", pa.float64()),
])


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--stop", type=date.fromisoformat, default=date(2026, 1, 1),
                        help="KST exclusive end")
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--stage-dir", type=Path, default=Path("staging"))
    parser.add_argument("--report-dir", type=Path, default=Path("full_rebuild_report"))
    return parser.parse_args()


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def missing(exc):
    e = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", None)
    return e in ("404", "NotFound", "NoSuchKey") or status == 404


def head(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if missing(e):
            return None
        raise


def list_under(client, bucket, prefix):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item["Key"]


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode("ascii")


def processing_hashes(pipeline, storage):
    sources = {"viirs_full_rebuild.py": Path(__file__),
               "viirs_pipeline.py": Path(pipeline.__file__),
               "viirs_s3.py": Path(storage.__file__)}
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent, prefix="report-") as temporary:
        staged = Path(temporary) / path.name
        staged.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(staged, path)


def verified_head(client, bucket, key):
    """Validate the stored content digest; this alone does not re-hash remote bytes."""
    obj = head(client, bucket, key)
    if obj is None:
        return None
    digest = obj.get("Metadata", {}).get("sha256", "")
    if (int(obj.get("ContentLength", 0)) <= 0
            or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", digest)):
        raise RuntimeError(f"Object lacks a valid size/SHA-256: {key}")
    return obj


def upload_verified(client, bucket, key, path, metadata, content_type, *, replace):
    from viirs_s3 import checksum

    size = path.stat().st_size
    if size <= 0 or size > MAX_BRONZE_SINGLE_PUT:
        raise ValueError(f"Output must be nonempty and at most 5 GiB for conditional publication: {path.name}")
    digest, md5 = checksum(path, with_md5=True)
    metadata = {**metadata, "sha256": digest}
    current = head(client, bucket, key) if replace else verified_head(client, bucket, key)
    if current is not None:
        if (current.get("Metadata", {}) == metadata and int(current["ContentLength"]) == size
                and current.get("ETag", "").strip('"') == md5.hexdigest()):
            return "SKIPPED_VERIFIED_EXISTING"
        if not replace:
            raise RuntimeError(f"Existing Bronze differs from the local original; not overwritten: {key}")
    # Conditional PUT prevents a second worker from silently replacing a changed object.
    condition = {"IfMatch": current["ETag"]} if current is not None else {"IfNoneMatch": "*"}
    try:
        with path.open("rb") as stream:
            client.put_object(Bucket=bucket, Key=key, Body=stream, ContentLength=size,
                              ContentType=content_type, Metadata=metadata,
                              ContentMD5=base64.b64encode(md5.digest()).decode("ascii"), **condition)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (409, 412) and code not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        existing = verified_head(client, bucket, key)
        if (existing is not None and int(existing["ContentLength"]) == size
                and existing.get("Metadata", {}) == metadata
                and existing.get("ETag", "").strip('"') == md5.hexdigest()):
            return "SKIPPED_CONCURRENT_VERIFIED"
        raise RuntimeError(f"Concurrent publication changed this object; rerun after the other worker: {key}") from None
    got = verified_head(client, bucket, key)
    if (got is None or int(got["ContentLength"]) != size or got.get("Metadata", {}) != metadata
            or got.get("ETag", "").strip('"') != md5.hexdigest()):
        raise RuntimeError(f"HEAD verification failed for R2 key: {key}")
    return "UPLOADED_VERIFIED"


@contextmanager
def rebuild_lock(stage):
    """Hold a process lock for the whole run; the OS releases it after crashes."""
    with (stage / ".full-rebuild.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another full rebuild already uses this staging directory") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def ensure_bronze(client, bucket, prefix, scene, assets, session, token, pipeline, temp):
    """Return existing/just-downloaded pair paths; never replace Bronze objects."""
    files = {}
    statuses = []
    for product in pipeline.PRODUCTS:
        filename, _ = assets[product]
        kst_day = pipeline.timestamp(scene).astimezone(pipeline.KST).strftime("%Y/%m/%d")
        key = f"{prefix}/bronze/{kst_day}/{filename}"
        local = temp / filename
        remote = verified_head(client, bucket, key)
        if remote is not None:
            if (int(remote.get("ContentLength", 0)) <= 0
                    or remote.get("Metadata", {}).get("nasa-granule") != filename):
                raise RuntimeError(f"Invalid existing Bronze object (not overwritten): {key}")
            # Download from R2 for spatial processing, not from NASA again.
            log(f"  Bronze exists in R2: {filename}")
            from viirs_s3 import fetch_file
            remote["verified_sha256"] = remote["Metadata"]["sha256"]
            fetch_file(client, bucket, key, local, remote, r2=True)
            statuses.append("SKIPPED_EXISTING")
        else:
            if not token:
                raise RuntimeError("EARTHDATA_TOKEN is needed to download missing Bronze granules")
            log(f"  NASA download: {filename}")
            downloaded = Path(pipeline.download(session, temp, scene, assets[product], token))
            if downloaded != local:
                shutil.move(str(downloaded), str(local))
            status = upload_verified(client, bucket, key, local, {"nasa-granule": filename},
                                     "application/x-netcdf", replace=False)
            statuses.append(status)
            log(f"  Bronze {status}: {filename}")
        files[product] = local
    return files, statuses


def scan_times(geo, lines):
    """Return scan start times (TAI93 seconds) and detector-line count per scan.

    Granule scan_start_time values are per scan, not actual pixel exposure times.
    Do not invent scan timestamps when the source data lacks them.
    """
    if "scan_line_attributes" not in geo.groups:
        raise ValueError("Geolocation lacks scan_line_attributes")
    group = geo.groups["scan_line_attributes"]
    if "scan_start_time" not in group.variables:
        raise ValueError("Geolocation lacks scan_line_attributes/scan_start_time")
    times = np.asarray(np.ma.filled(group["scan_start_time"][:], np.nan), dtype="float64")
    if times.ndim != 1 or times.size == 0 or lines % times.size:
        raise ValueError(f"Cannot map {lines} image lines to scan_start_time shape {times.shape}")
    if not np.isfinite(times).all() or (times <= 0).any():
        raise ValueError("Invalid scan_start_time values; refusing to fabricate timestamps")
    return times, lines // len(times)


def extract_scene(image_path, geo_path, swath, dongs, writer, pipeline):
    """Stream selected Korean administrative-area pixels in small scanline slices."""
    from netCDF4 import Dataset

    total = finite = flagged = valid = 0
    first_utc = pipeline.timestamp(swath)
    left, bottom, right, top = dongs.total_bounds
    indexed_codes = dongs.index.to_numpy()
    source_metadata = pipeline.source_metadata(image_path, geo_path, swath)
    with Dataset(image_path) as img, Dataset(geo_path) as geo:
        band = img["observation_data"]["I05"]
        band.set_auto_scale(False)
        quality = img["observation_data"]["I05_quality_flags"]
        quality.set_auto_maskandscale(False)
        lut = np.ma.filled(img["observation_data"]["I05_brightness_temperature_lut"][:].astype("float32"), np.nan)
        lon, lat = geo["geolocation_data"]["longitude"], geo["geolocation_data"]["latitude"]
        if band.shape != quality.shape or band.shape != lon.shape or band.shape != lat.shape:
            raise ValueError("Image, quality and geolocation shapes differ")
        pipeline.limit_chunk_cache(band, quality, lon, lat)
        times, per_scan = scan_times(geo, band.shape[0])
        for start in range(0, band.shape[0], 16):
            rowslice = slice(start, min(start + 16, band.shape[0]))
            x = np.ma.filled(lon[rowslice].astype("float64"), np.nan)
            y = np.ma.filled(lat[rowslice].astype("float64"), np.nan)
            row, col = np.nonzero((left <= x) & (x <= right) & (bottom <= y) & (y <= top))
            if not row.size:
                continue
            import geopandas as gpd
            pts = gpd.points_from_xy(x[row, col], y[row, col])
            pixel, dong_idx = dongs.sindex.query(pts, predicate="within")
            if not pixel.size:
                continue
            if np.unique(pixel).size != pixel.size:
                raise ValueError("Overlapping administrative polygons at pixel center")
            row, col = row[pixel], col[pixel]
            selected = band[rowslice][row, col]
            raw = np.ma.getdata(selected)
            good = (~np.ma.getmaskarray(selected)) & (raw >= 0) & (raw < lut.size)
            t = np.full(len(row), np.nan, dtype="float32")
            t[good] = lut[raw[good]]
            flag = np.asarray(np.ma.getdata(quality[rowslice][row, col]), dtype="uint16")
            abrow = row + start
            scan_index = abrow // per_scan
            scan_start = times[scan_index]
            if not np.isfinite(scan_start).all():
                raise ValueError("Nonfinite scan time at an included pixel")
            cols = [
                pa.array(indexed_codes[dong_idx], type=pa.string()),
                pa.array(abrow, type=pa.int32()), pa.array(col, type=pa.int32()),
                pa.array(x[row, col]), pa.array(y[row, col]),
                pa.array(t, mask=~np.isfinite(t)), pa.array(flag, type=pa.uint16()),
                pa.array([swath] * len(row), type=pa.string()),
                pa.array([first_utc] * len(row), type=pa.timestamp("us", tz="UTC")),
                pa.array(scan_index, type=pa.int32()),
                pa.array(scan_start, type=pa.float64()),
            ]
            writer.write_table(pa.Table.from_arrays(cols, schema=writer.schema))
            total += len(row)
            finite += int(np.isfinite(t).sum())
            flagged += int(np.count_nonzero(flag))
            valid += int((np.isfinite(t) & (flag == 0)).sum())
    return {"pixel_count": total, "finite_temperature": finite,
            "flagged": flagged, "valid_flag_zero_temperature": valid,
            "source_metadata": source_metadata}


def build_gold(silver_path, dest, day, boundary_version, sources):
    """Aggregate per dong × scene, keeping scan-time bounds; process Parquet in batches."""
    agg = {}  # (dong_code, swath_id) -> count, finite, flagged, valid, sum, min, max, scan_min, scan_max, scene_time
    with pq.ParquetFile(silver_path) as source:
        source_metadata = {k.decode(): v.decode() for k, v in (source.schema_arrow.metadata or {}).items()}
        for batch in source.iter_batches(batch_size=8192,
                columns=["dong_code", "swath_id", "swath_start_utc", "scan_start_time_tai93_s",
                         "i05_brightness_temperature_k", "i05_quality_flags"]):
            codes = batch.column(0).to_pylist()
            scenes = batch.column(1).to_pylist()
            times = batch.column(2).to_pylist()
            scan = batch.column(3).to_pylist()
            temps = batch.column(4).to_pylist()
            flags = batch.column(5).to_pylist()
            for code, scene, utc, st, temp, flag in zip(codes, scenes, times, scan, temps, flags):
                k = (code, scene)
                # 0=count 1=finite 2=flagged 3=valid 4=sum 5=min 6=max 7=firstscan 8=lastscan 9=sceneutc
                a = agg.setdefault(k, [0, 0, 0, 0, 0., None, None, st, st, utc])
                a[0] += 1
                a[7] = min(a[7], st)
                a[8] = max(a[8], st)
                a[2] += int(flag != 0)
                if temp is not None and math.isfinite(temp):
                    a[1] += 1
                    if flag == 0:
                        a[3] += 1
                        a[4] += temp
                        a[5] = temp if a[5] is None else min(a[5], temp)
                        a[6] = temp if a[6] is None else max(a[6], temp)
    out = []
    for (code, scene), a in sorted(agg.items()):
        out.append({
            "date": day, "boundary_version": boundary_version, "dong_code": code,
            "swath_id": scene, "swath_start_utc": a[9],
            "first_scan_start_time_tai93_s": a[7], "last_scan_start_time_tai93_s": a[8],
            "pixel_count": a[0], "finite_temperature_count": a[1], "flagged_count": a[2],
            "valid_count": a[3], "temperature_mean_k": a[4] / a[3] if a[3] else None,
            "temperature_min_k": a[5], "temperature_max_k": a[6],
            "valid_fraction": a[3] / a[0] if a[0] else None,
        })
    meta = {"pipeline_version": VERSION, "date_kst": str(day), "boundary_version": boundary_version,
            "units": "Kelvin; TAI93 seconds", "aggregation": "dong_code x swath_id",
            "time_accuracy": "scan_start_time is scan-level, not exact pixel exposure time",
            "temperature_valid_rule": "finite temperature AND quality_flags == 0",
            "rainfall_ground_truth": "none", "source_silver": sources,
            "source_silver_sha256": base64.b64decode(hash_file(silver_path)).hex(),
            "processing_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "valid_fraction_denominator": "all Silver pixels in this dong and swath",
            "cloud_qc_status": "unverified", "resampling": "none"}
    for name in ("boundary_sha256", "processing_code_sha256", "processing_code_hashes", "input_fingerprint"):
        if name in source_metadata:
            meta[name] = source_metadata[name]
    table = pa.Table.from_pylist(out, schema=GOLD_SCHEMA.with_metadata({k: str(v) for k, v in meta.items()}))
    pq.write_table(table, dest, compression="zstd", row_group_size=1024)
    if pq.ParquetFile(dest).metadata.num_rows != len(out):
        raise RuntimeError("Gold local row count mismatch")
    return len(out)


def daily_paths(prefix, boundary_version, day):
    dd = day.strftime("%Y/%m/%d")
    base = f"{boundary_version}/{dd}"
    return {
        "silver": f"{prefix}/silver/{base}/{SILVER_FILE}",
        "quality": f"{prefix}/quality/{base}/{QUALITY_FILE}",
        "gold": f"{prefix}/gold/{base}/{GOLD_FILE}",
    }


def daily_fingerprint(scenes, assets_by_scene, boundary_hash, code_hash):
    names = [(s, *(assets_by_scene[s][p][0] for p in sorted(assets_by_scene[s]))) for s in sorted(scenes)]
    return hashlib.sha256(json.dumps([VERSION, names, boundary_hash, code_hash],
                                     sort_keys=True).encode()).hexdigest()


def old_keys(client, bucket, paths):
    """Return any legacy / per-scene objects within this day, excluding current v2 keys."""
    deleted = []
    for label, fresh in paths.items():
        stem = fresh.rsplit("/", 1)[0] + "/"
        suffix = ".json" if label == "quality" else ".parquet"
        # Delete only old derived data files, never arbitrary keys in a day folder.
        legacy_name = re.compile(r"A\d{7}\.\d{4}" + re.escape(suffix))
        deleted.extend(k for k in list_under(client, bucket, stem)
                       if legacy_name.fullmatch(k.removeprefix(stem)))
    return sorted(set(deleted))


def delete_legacy(client, bucket, obsolete):
    """Delete only legacy derived outputs AFTER verifying all new daily outputs."""
    for key in obsolete:
        client.delete_object(Bucket=bucket, Key=key)
    return len(obsolete)


def publish_completion(client, bucket, paths, day, boundary_version, fingerprint, metadata, stage):
    """Publish last, so readers can detect a partial three-object replacement."""
    objects = {}
    for label, key in paths.items():
        obj = verified_head(client, bucket, key)
        if (obj is None or obj["Metadata"].get("input-fingerprint") != fingerprint
                or obj["Metadata"].get("pipeline-version") != VERSION):
            raise RuntimeError(f"Daily output set is incomplete: {key}")
        objects[label] = {"key": key, "size_bytes": int(obj["ContentLength"]),
                          "sha256": obj["Metadata"]["sha256"]}
    manifest = {"manifest_version": 1, "pipeline_version": VERSION, "date_kst": str(day),
                "boundary_version": boundary_version, "input_fingerprint": fingerprint,
                "objects": objects}
    manifest_key = paths["quality"].rsplit("/", 1)[0] + "/completion.json"
    with TemporaryDirectory(dir=stage, prefix="completion-") as work:
        path = Path(work) / "completion.json"
        path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        upload_verified(client, bucket, manifest_key, path, metadata, "application/json", replace=True)


def one_day(client, bucket, prefix, day, scenes, assets, dongs, bound_path,
            bound_version, boundary_hash, script_hash, session, token, pipeline, stage,
            *, code_hashes=None):
    if not scenes:
        return {"status": "NO_PAIRED_SCENE", "scenes": 0, "pixels": 0, "obsolete_deleted": 0}
    paths = daily_paths(prefix, bound_version, day)
    fp = daily_fingerprint(scenes, assets, boundary_hash, script_hash)
    needed_meta = {"pipeline-version": VERSION, "input-fingerprint": fp}
    complete = all((h := head(client, bucket, k)) is not None
                   and h.get("Metadata", {}).get("pipeline-version") == VERSION
                   and h.get("Metadata", {}).get("input-fingerprint") == fp
                   and re.fullmatch(r"[A-Za-z0-9+/]{43}=", h.get("Metadata", {}).get("sha256", ""))
                   and int(h.get("ContentLength", 0)) > 0
                   for k in paths.values())
    if complete:
        publish_completion(client, bucket, paths, day, bound_version, fp, needed_meta, stage)
        outdated = old_keys(client, bucket, paths)
        deleted = delete_legacy(client, bucket, outdated)
        return {"status": "SKIPPED_VERIFIED_EXISTING", "scenes": len(scenes),
                "pixels": -1, "obsolete_deleted": deleted}

    with TemporaryDirectory(dir=stage, prefix="all-viirs-") as tmp:
        tmp = Path(tmp)
        silver_file = tmp / SILVER_FILE
        quality_file = tmp / QUALITY_FILE
        gold_file = tmp / GOLD_FILE
        silver_meta = {"pipeline_version": VERSION, "kst_date": str(day),
                       "boundary_version": bound_version, "swaths": json.dumps(sorted(scenes)),
                       "scan_time_units": "TAI93 seconds", "scan_time_precision": "scan start; not per-pixel exposure",
                       "coordinate_crs": "EPSG:4326", "temperature_units": "K",
                       "sampling": "native I5 pixel centers; no resampling",
                       "source_assets": json.dumps({s: {p: assets[s][p][0] for p in pipeline.PRODUCTS} for s in scenes}, sort_keys=True),
                       "boundary_sha256": boundary_hash, "processing_code_sha256": script_hash,
                       "processing_code_hashes": json.dumps(code_hashes or {}, sort_keys=True),
                       "cloud_qc_status": "unverified", "input_fingerprint": fp}
        schema = SILVER_SCHEMA.with_metadata(silver_meta)
        stats = {}
        with pq.ParquetWriter(silver_file, schema, compression="zstd") as writer:
            for idx, scene in enumerate(scenes, 1):
                log(f"[{day}] scene {idx}/{len(scenes)} {scene}")
                with TemporaryDirectory(dir=stage, prefix="scene-") as scratch:
                    files, bronze_stat = ensure_bronze(client, bucket, prefix, scene, assets[scene],
                                                       session, token, pipeline, Path(scratch))
                    result = extract_scene(files[pipeline.PRODUCTS[0]], files[pipeline.PRODUCTS[1]],
                                           scene, dongs, writer, pipeline)
                    result["source_sha256"] = {product: hash_file(path) for product, path in files.items()}
                    result["bronze_status"] = bronze_stat
                    stats[scene] = result
                    log(f"[{day}] {scene} matched administrative pixels={result['pixel_count']}")
            writer.add_key_value_metadata({"source_sha256": json.dumps(
                {scene: item["source_sha256"] for scene, item in stats.items()}, sort_keys=True)})

        actual_rows = pq.ParquetFile(silver_file).metadata.num_rows
        expected_rows = sum(item["pixel_count"] for item in stats.values())
        if actual_rows != expected_rows:
            raise RuntimeError(f"Silver row count mismatch {actual_rows} != {expected_rows}")
        quality = {"pipeline_version": VERSION, "date_kst": str(day),
                   "boundary_version": bound_version, "source_scene_count": len(scenes),
                   "pixel_count": actual_rows, "per_scene": stats,
                   "finite_temperature": sum(item["finite_temperature"] for item in stats.values()),
                   "flagged": sum(item["flagged"] for item in stats.values()),
                   "valid_flag_zero_temperature": sum(item["valid_flag_zero_temperature"] for item in stats.values()),
                   "input_fingerprint": fp, "processing_code_sha256": script_hash,
                   "processing_code_hashes": code_hashes or {},
                   "boundary_sha256": boundary_hash, "cloud_qc_status": "unverified",
                   "valid_fraction_denominator": "all Silver pixels assigned to each dong and swath",
                   "status": "EMPTY_LAND_OBSERVATION" if not actual_rows else "GENERATED",
                   "warning": "A CMR BBOX match can contain no pixels inside Korean administrative polygons",
                   "scan_time": "TAI93 scan start, duplicated per scan pixel; exact pixel exposure unknown"}
        quality_file.write_text(json.dumps(quality, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        gold_rows = build_gold(silver_file, gold_file, day, bound_version, paths["silver"])
        log(f"[{day}] prepared Silver={actual_rows} pixels Gold={gold_rows} groups")

        # Do not delete old objects until all 3 new files are successfully written and verified.
        srcs = [("silver", silver_file, "application/vnd.apache.parquet"),
                ("quality", quality_file, "application/json"),
                ("gold", gold_file, "application/vnd.apache.parquet")]
        for label, local, mime in srcs:
            result = upload_verified(client, bucket, paths[label], local, needed_meta, mime,
                                     replace=True)
            log(f"[{day}] {label.upper()} {result} {paths[label]}")
        # Verify that all three replacements have the correct current fingerprint.
        for key in paths.values():
            h = head(client, bucket, key)
            if (h is None or h.get("Metadata", {}).get("input-fingerprint") != fp
                    or h.get("Metadata", {}).get("pipeline-version") != VERSION):
                raise RuntimeError(f"R2 replacement check failed: {key}")
        publish_completion(client, bucket, paths, day, bound_version, fp, needed_meta, stage)
        obsolete = old_keys(client, bucket, paths)
        deleted = delete_legacy(client, bucket, obsolete)
        return {"status": "REPLACED_VERIFIED", "scenes": len(scenes), "pixels": actual_rows,
                "gold_rows": gold_rows, "obsolete_deleted": deleted}


def main():
    args = parse_args()
    if not date(2024, 1, 1) <= args.start < args.stop <= date(2026, 1, 1) or not 1 <= args.chunk_days <= 31:
        raise ValueError("Invalid date range or chunk-days")
    if not args.env.is_file():
        raise RuntimeError(f"Missing .env: {args.env}")
    load_dotenv(args.env, override=False)
    missing_env = [k for k in REQUIRED if not os.getenv(k)]
    if missing_env:
        raise RuntimeError("Missing R2 configuration: " + ", ".join(missing_env))
    prefix = os.getenv("R2_PREFIX", "viirs-daily").strip("/")
    if not prefix or any(part in ("", ".", "..") for part in prefix.split("/")):
        raise RuntimeError("Invalid R2_PREFIX")
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    import viirs_pipeline as pipeline
    import viirs_s3 as existing
    if len(pipeline.PRODUCTS) != 2:
        raise RuntimeError("Expected 2 VIIRS products in viirs_pipeline.PRODUCTS")
    client = existing.storage_client(SimpleNamespace(r2_account_id=os.environ["R2_ACCOUNT_ID"],
                                                     profile=None, region=None))
    bucket = os.environ["R2_BUCKET"]
    client.head_bucket(Bucket=bucket)
    token = pipeline.earthdata_token()
    boundaries = {}
    boundary_hashes = {}
    root = Path("boundaries")
    for year in range(args.start.year, (args.stop - timedelta(days=1)).year + 1):
        path = root / f"bnd_dong_00_{year}_2Q.shp"
        if not path.is_file():
            raise RuntimeError(f"Missing SGIS boundary file: {path}")
        geo = pipeline.boundaries(path, "ADM_CD")
        if str(geo.crs).upper() != "EPSG:4326":
            raise RuntimeError(f"Boundary CRS must be EPSG:4326: {path}")
        boundaries[year] = geo
        # Include all shapefile components; avoid stale output after boundary changes.
        h = hashlib.sha256()
        for component in sorted(root.glob(f"{path.stem}.*")):
            h.update(component.name.encode())
            h.update(component.read_bytes())
        boundary_hashes[year] = h.hexdigest()
    code_hashes = processing_hashes(pipeline, existing)
    script_hash = hashlib.sha256(json.dumps(code_hashes, sort_keys=True).encode()).hexdigest()

    file_path = args.report_dir / "daily_results.csv"
    total = Counter()
    log(f"RUN FULL 2024-2025 REBUILD {args.start} ... {args.stop} (KST exclusive)")
    log(f"R2 bucket={bucket} prefix={prefix}; legacy Silver/Quality/Gold will be DELETED per day after replacement")
    log("Bronze files already present will be re-used, not overwritten. No rainfall label inferred.")
    with rebuild_lock(args.stage_dir), requests.Session() as session, file_path.open("a", newline="", encoding="utf-8") as output:
        session.mount("https://", HTTPAdapter(max_retries=Retry(total=4, backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504))))
        fieldnames = ["date", "status", "scene_count", "silver_pixels", "gold_rows", "obsolete_deleted", "detail"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        if file_path.stat().st_size == 0:
            writer.writeheader()
            output.flush()
        def report(day, status, scenes=0, pixels="", gold="", deleted=0, detail=""):
            record = dict(date=str(day), status=status, scene_count=scenes,
                          silver_pixels=pixels, gold_rows=gold,
                          obsolete_deleted=deleted, detail=str(detail)[:180])
            writer.writerow(record)
            output.flush()
            total[status] += 1
            write_json_atomic(args.report_dir / "days" / f"{day}.json", {
                **record, "pipeline_version": VERSION, "processing_code_hashes": code_hashes,
                "updated_at_utc": datetime.now(timezone.utc).isoformat()})
            write_json_atomic(args.report_dir / "summary.json", {
                "version": VERSION, "start_kst": str(args.start), "stop_kst_exclusive": str(args.stop),
                "status_counts_this_run": dict(total), "report": str(file_path),
                "run_state": "running", "last_processed_date": str(day),
                "processing_code_hashes": code_hashes})
            log(f"{day} {status} scenes={scenes} pixels={pixels} deleted_old={deleted} {detail}")
        current = args.start
        while current < args.stop:
            end = min(current + timedelta(days=args.chunk_days), args.stop)
            utc_start = datetime.combine(current, time.min, pipeline.KST).astimezone(timezone.utc)
            utc_stop = datetime.combine(end, time.min, pipeline.KST).astimezone(timezone.utc)
            log(f"CMR: {current} .. {end} (exclusive)")
            try:
                image = pipeline.search(session, pipeline.PRODUCTS[0], utc_start, utc_stop)
                geo = pipeline.search(session, pipeline.PRODUCTS[1], utc_start, utc_stop)
            except Exception as exc:
                log(f"CMR FAILED for {current}..{end}: {type(exc).__name__}: {exc}")
                day = current
                while day < end:
                    report(day, "ERROR_CMR")
                    day += timedelta(days=1)
                current = end
                continue
            paired = image.keys() & geo.keys()
            unmatched = image.keys() ^ geo.keys()
            byday = defaultdict(list)
            assets = {}
            for scene in sorted(paired):
                when = pipeline.timestamp(scene).astimezone(pipeline.KST).date()
                if current <= when < end:
                    byday[when].append(scene)
                    assets[scene] = {pipeline.PRODUCTS[0]: image[scene], pipeline.PRODUCTS[1]: geo[scene]}
            unmatched_days = {pipeline.timestamp(scene).astimezone(pipeline.KST).date()
                              for scene in unmatched}
            day = current
            while day < end:
                if day in unmatched_days:
                    report(day, "ERROR_UNPAIRED_CMR", len(byday[day]), detail="CMR returned an unmatched product")
                    day += timedelta(days=1)
                    continue
                if day not in byday:
                    report(day, "NO_PAIRED_CMR", detail="No matching original product pair; old outputs untouched")
                    day += timedelta(days=1)
                    continue
                try:
                    r = one_day(client, bucket, prefix, day, sorted(byday[day]), assets,
                                boundaries[day.year], root / f"bnd_dong_00_{day.year}_2Q.shp",
                                f"sgis-{day.year}-2q", boundary_hashes[day.year], script_hash,
                                session, token, pipeline, args.stage_dir, code_hashes=code_hashes)
                    report(day, r["status"], r["scenes"], r["pixels"], r.get("gold_rows", ""), r["obsolete_deleted"])
                except Exception as exc:
                    # Keep old derived data on a failed day rather than deleting the only copy.
                    report(day, "ERROR_DAY", len(byday[day]), detail=f"{type(exc).__name__}: {exc}")
                day += timedelta(days=1)
            current = end
    summary = {"version": VERSION, "start_kst": str(args.start), "stop_kst_exclusive": str(args.stop),
               "status_counts_this_run": dict(total), "report": str(file_path),
               "run_state": "finished", "processing_code_hashes": code_hashes,
               "note": "An ERROR_DAY means old files may remain; rerun to retry failed days. "
                       "NO_PAIRED_CMR dates are not fabricated."}
    write_json_atomic(args.report_dir / "summary.json", summary)
    log("FINISHED: " + json.dumps(total))
    return 1 if total.get("ERROR_DAY") or total.get("ERROR_CMR") or total.get("ERROR_UNPAIRED_CMR") else 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
