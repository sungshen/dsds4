"""Prepare native GK2A patches and strictly aligned retrospective rain samples.

This module has no torch dependency. It never manufactures missing observations,
converts packed DN into temperature, or treats a station label as a dong label.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import rasterio
from rasterio.windows import Window, transform as window_transform
from rasterio.warp import transform

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from crop_gk2a_silver import grid  # Existing, independently checked native KO grid.

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
VERSION = "gk2a-seoul-station-patches-v1"
AVAILABILITY_MODE = "retrospective_observation_time"
STATION_ID = "108"
STATION_LAT = 37.57142
STATION_LON = 126.9658
FEATURES = ("temperature_c", "humidity_pct", "pressure_hpa", "wind_speed_m_s",
            "wind_direction_deg", "past_rain_1h_mm")
PATCH_FIELDS = ("patch_id", "observed_at_utc", "observed_at_kst", "path", "sha256",
                "metadata_path", "metadata_sha256", "source_path", "source_sha256",
                "valid_pixel_count", "height", "width", "availability_mode")
SAMPLE_FIELDS = ("sample_id", "station_id", "issue_time_kst", "target_start_kst",
                 "target_end_kst", "label", "rain_1h_mm", "split", "frame_paths",
                 "frame_sha256", "frame_times_utc", "availability_mode", "numeric_available_at_kst", *FEATURES)
DEFAULT_SPLITS = (
    ("train", datetime(2024, 1, 1, tzinfo=KST), datetime(2025, 1, 1, tzinfo=KST)),
    ("val", datetime(2025, 1, 1, tzinfo=KST), datetime(2025, 7, 1, tzinfo=KST)),
    ("test", datetime(2025, 7, 1, tzinfo=KST), datetime(2026, 1, 1, tzinfo=KST)),
)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Observation and label times must include a timezone")
    return result.astimezone(UTC)


def exact_hour(value: datetime) -> bool:
    return value.minute == value.second == value.microsecond == 0


def relative(path: Path, parent: Path) -> str:
    return os.path.relpath(Path(path).resolve(), Path(parent).resolve())


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=".json-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                            prefix=".csv-", delete=False) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def manifest_rows(path: Path) -> list[dict]:
    path = Path(path).resolve()
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = value if isinstance(value, list) else value.get("assets", value.get("records"))
        if not isinstance(rows, list):
            raise ValueError("JSON manifest needs an assets/records list")
        return rows
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def source_records(manifest: Path) -> list[dict]:
    """New path fields are manifest-relative; legacy file_path is repo-relative."""
    manifest = Path(manifest).resolve()
    indexed = {}
    for row in manifest_rows(manifest):
        stamp = parse_time(row.get("observed_at_utc") or row.get("observation_time_utc") or "")
        sha = str(row.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("Source manifest requires a hex SHA-256 for every original")
        if row.get("path"):
            path = Path(row["path"]).expanduser()
            if not path.is_absolute():
                path = manifest.parent / path
        elif row.get("file_path"):
            path = Path(row["file_path"]).expanduser()
            if not path.is_absolute():
                path = ROOT / path
        else:
            raise ValueError("Source manifest requires path or legacy file_path")
        record = {"time": stamp, "path": path.resolve(), "sha256": sha}
        previous = indexed.get(stamp)
        if previous is not None and previous["sha256"] != sha:
            raise ValueError(f"Ambiguous different originals at {stamp.isoformat()}")
        indexed.setdefault(stamp, record)
    return [indexed[stamp] for stamp in sorted(indexed)]


def extract_patch(source: dict, output: Path, *, patch_size=64,
                  latitude=STATION_LAT, longitude=STATION_LON) -> dict:
    if not isinstance(patch_size, int) or not 2 <= patch_size <= 900 or patch_size % 2:
        raise ValueError("patch_size must be an even native-pixel width in [2, 900]")
    output = Path(output).resolve()
    original, stamp = Path(source["path"]), source["time"]
    if digest(original) != source["sha256"]:
        raise ValueError(f"Original SHA-256 mismatch: {original.name}")
    parameters = {"version": VERSION, "latitude": latitude, "longitude": longitude,
                  "patch_size": patch_size, "processing_code_sha256": digest(Path(__file__))}
    config_sha = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    identity = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{source['sha256'][:12]}-{config_sha[:12]}"
    patch_path = output / "patches" / f"{identity}.npz"
    metadata_path = output / "metadata" / f"{identity}.json"
    if patch_path.exists() or metadata_path.exists():
        if not patch_path.is_file() or not metadata_path.is_file():
            raise ValueError(f"Incomplete cached patch: {identity}")
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("patch_sha256") != digest(patch_path):
            raise ValueError(f"Cached patch SHA-256 mismatch: {identity}")
    else:
        with rasterio.Env(GDAL_NETCDF_BOTTOMUP="NO"), rasterio.open(original) as src:
            crs, affine = grid(src)
            filename = src.tags().get("NC_GLOBAL#file_name", "")
            if not filename.endswith(stamp.strftime("_%Y%m%d%H%M.nc")):
                raise ValueError("NetCDF acquisition time differs from source manifest")
            xs, ys = transform("EPSG:4326", crs, [longitude], [latitude])
            station_row, station_col = rasterio.transform.rowcol(affine, xs[0], ys[0])
            row0, col0 = int(station_row) - patch_size // 2, int(station_col) - patch_size // 2
            if row0 < 0 or col0 < 0 or row0 + patch_size > src.height or col0 + patch_size > src.width:
                raise ValueError("Station context patch extends outside native source")
            window = Window(col0, row0, patch_size, patch_size)
            raw = src.read(1, window=window, masked=True)
            dn = np.asarray(raw.data, dtype="uint16")
            nodata = np.ma.getmaskarray(raw) | (dn == 65535)
            upper_bits = dn > 8191
            valid = (~nodata & ~upper_bits).astype("uint8")
            metadata = {**parameters, "patch_id": identity,
                        "observed_at_utc": stamp.isoformat(), "observed_at_kst": stamp.astimezone(KST).isoformat(),
                        "source_path": str(original), "source_sha256": source["sha256"],
                        "source_product_filename": filename, "units": "packed_DN", "calibration": "none",
                        "crs_wkt": crs.to_wkt(), "source_transform": list(affine)[:6],
                        "patch_transform": list(window_transform(window, affine))[:6],
                        "native_pixel_size_m": 2000, "source_row": row0, "source_column": col0,
                        "station_source_row": int(station_row), "station_source_column": int(station_col),
                        "height": patch_size, "width": patch_size, "valid_pixel_count": int(valid.sum()),
                        "source_nodata_count": int(nodata.sum()),
                        "upper_bits_excluded_count": int((upper_bits & ~nodata).sum()),
                        "cloud_qc_status": "unverified", "resampling": "none",
                        "availability_mode": AVAILABILITY_MODE,
                        "label_scope": "Seoul station 108 only; no dong rainfall labels"}
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=patch_path.parent, suffix=".npz", delete=False) as stream:
            temporary = Path(stream.name)
            np.savez_compressed(stream, dn=dn, valid=valid)
        try:
            os.replace(temporary, patch_path)
        finally:
            temporary.unlink(missing_ok=True)
        metadata["patch_sha256"] = digest(patch_path)
        write_json(metadata_path, metadata)
    return {"patch_id": identity, "observed_at_utc": stamp.isoformat(),
            "observed_at_kst": stamp.astimezone(KST).isoformat(),
            "path": relative(patch_path, output), "sha256": metadata["patch_sha256"],
            "metadata_path": relative(metadata_path, output), "metadata_sha256": digest(metadata_path),
            "source_path": str(original), "source_sha256": source["sha256"],
            "valid_pixel_count": metadata["valid_pixel_count"], "height": metadata["height"],
            "width": metadata["width"], "availability_mode": AVAILABILITY_MODE}


def build_patches(source_manifest: Path, output: Path, *, patch_size=64,
                  latitude=STATION_LAT, longitude=STATION_LON) -> dict:
    output = Path(output).resolve()
    records = source_records(source_manifest)
    if not records:
        raise ValueError("Source manifest contains no observations")
    patches = [extract_patch(record, output, patch_size=patch_size, latitude=latitude,
                             longitude=longitude) for record in records]
    write_csv(output / "patch_manifest.csv", PATCH_FIELDS, patches)
    exact = [record for record in records if exact_hour(record["time"])]
    summary = {"processing_version": VERSION, "source_manifest": str(Path(source_manifest).resolve()),
               "source_manifest_sha256": digest(Path(source_manifest)), "patch_count": len(patches),
               "exact_hour_observations": len(exact), "non_hour_observations": len(records) - len(exact),
               "first_observed_at_utc": records[0]["time"].isoformat(),
               "last_observed_at_utc": records[-1]["time"].isoformat(),
               "patch_shape": [patch_size, patch_size], "units": "packed_DN",
               "availability_mode": AVAILABILITY_MODE, "cloud_qc_status": "unverified"}
    write_json(output / "patch_summary.json", summary)
    return summary


def load_patches(manifest: Path) -> dict:
    manifest = Path(manifest).resolve()
    indexed = {}
    common_grid = None
    for record in manifest_rows(manifest):
        stamp = parse_time(record["observed_at_utc"])
        path = (manifest.parent / record["path"]).resolve()
        if digest(path) != record["sha256"]:
            raise ValueError("Patch checksum differs from manifest")
        if record.get("metadata_path"):
            metadata_path = manifest.parent / record["metadata_path"]
            if digest(metadata_path) != record.get("metadata_sha256"):
                raise ValueError("Patch metadata checksum differs from manifest")
            metadata = json.loads(metadata_path.read_text())
            signature = json.dumps([metadata['crs_wkt'], metadata['patch_transform'], metadata['height'], metadata['width']])
            if common_grid is not None and signature != common_grid:
                raise ValueError("Image patches are on different native grids")
            common_grid = signature
        if stamp in indexed:
            raise ValueError("Multiple patches share an observation timestamp")
        indexed[stamp] = {**record, "absolute_path": path}
    return indexed


def finite_number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def rain_value(row: dict):
    value = finite_number(row.get("rain_1h_mm"))
    if value is None or value < 0:
        return None
    # Collector can normalize provider-specific QC into an explicit boolean.
    if str(row.get("rain_valid", "true")).strip().lower() not in ("true", "1", "yes"):
        return None
    return value


def load_station(path: Path | None) -> dict:
    if path is None or not Path(path).is_file():
        return {}
    indexed = {}
    for row in manifest_rows(Path(path)):
        if str(row.get("station_id", "")).strip() != STATION_ID:
            raise ValueError("Only Seoul station 108 is supported by this experiment")
        stamp = parse_time(row["observed_at_kst"])
        if row.get("source_path"):
            original = Path(path).resolve().parent / row["source_path"]
            if digest(original) != row.get("source_sha256"):
                raise ValueError("Station Bronze checksum differs from label lineage")
        if not exact_hour(stamp):
            raise ValueError("Station hourly labels must use exact hour timestamps")
        if row.get("rain_window_end_kst") and parse_time(row["rain_window_end_kst"]) != stamp:
            raise ValueError("Rain accumulation end differs from station observation time")
        if row.get("rain_window_start_kst") and parse_time(row["rain_window_start_kst"]) != stamp-timedelta(hours=1):
            raise ValueError("Rain target is not an exact preceding one-hour accumulation")
        if stamp in indexed and indexed[stamp] != row:
            raise ValueError("Conflicting station observations at the same hour")
        indexed[stamp] = row
    return indexed


def assigned_split(issue: datetime, *, splits=DEFAULT_SPLITS):
    history_start, target_end = issue - timedelta(hours=3), issue + timedelta(hours=1)
    for name, start, stop in splits:
        if history_start >= start and target_end < stop:
            return name
    return None


def numeric_features(row: dict | None, issue: datetime) -> dict:
    result = dict.fromkeys((*FEATURES, "numeric_available_at_kst"), "")
    if not row or not row.get("available_at_kst"):
        return result
    if parse_time(row["available_at_kst"]) > issue:
        return result
    result["numeric_available_at_kst"] = row["available_at_kst"]
    for name in FEATURES:
        if name == "past_rain_1h_mm":
            value = rain_value(row)
        else:
            value = finite_number(row.get(name))
            flag = row.get(name + "_valid", "true")
            if str(flag).strip().lower() not in ("true", "1", "yes"):
                value = None
        if value is not None:
            result[name] = value
    return result


def build_samples(patch_manifest: Path, station_csv: Path | None, output: Path, *, threshold=30.0,
                  splits=DEFAULT_SPLITS) -> dict:
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("Heavy-rain threshold must be finite and positive")
    output = Path(output).resolve()
    patches, station = load_patches(patch_manifest), load_station(station_csv)
    counts, rows = Counter(), []
    for issue in sorted(patches):
        counts["candidate_observations"] += 1
        if not exact_hour(issue):
            counts["excluded_non_hour_observation"] += 1
            continue
        stamps = [issue - timedelta(hours=hours) for hours in (3, 2, 1, 0)]
        if any(stamp not in patches for stamp in stamps):
            counts["missing_four_frame_history"] += 1
            continue
        frames = [patches[stamp] for stamp in stamps]
        if any(int(frame.get("valid_pixel_count", 0)) <= 0 for frame in frames):
            counts["empty_image_frame"] += 1
            continue
        split = assigned_split(issue, splits=splits)
        if split is None:
            counts["purged_split_boundary"] += 1
            continue
        end = issue + timedelta(hours=1)
        label_row = station.get(end)
        rain = rain_value(label_row) if label_row is not None else None
        if rain is None:
            counts["missing_or_invalid_target_rain"] += 1
            continue
        sample_id = hashlib.sha256(json.dumps([STATION_ID, issue.isoformat(), threshold,
                                  [frame["sha256"] for frame in frames]], sort_keys=True).encode()).hexdigest()
        rows.append({"sample_id": sample_id, "station_id": STATION_ID,
                     "issue_time_kst": issue.astimezone(KST).isoformat(),
                     "target_start_kst": issue.astimezone(KST).isoformat(),
                     "target_end_kst": end.astimezone(KST).isoformat(),
                     "rain_1h_mm": rain, "label": int(rain >= threshold), "split": split,
                     "frame_paths": json.dumps([relative(frame["absolute_path"], output) for frame in frames]),
                     "frame_sha256": json.dumps([frame["sha256"] for frame in frames]),
                     "frame_times_utc": json.dumps([stamp.isoformat() for stamp in stamps]),
                     "availability_mode": AVAILABILITY_MODE,
                     **numeric_features(station.get(issue), issue)})
        counts["samples"] += 1
    write_csv(output / "samples.csv", SAMPLE_FIELDS, rows)
    summary = {"processing_version": VERSION, "station_id": STATION_ID,
               "station_latitude": STATION_LAT, "station_longitude": STATION_LON,
               "threshold_mm_per_hour": threshold, "target_window": "(issue_time, issue_time + 1h]",
               "frame_offsets_hours": [-3, -2, -1, 0], "availability_mode": AVAILABILITY_MODE,
               "numeric_feature_policy": "blank unless explicit available_at_kst <= issue_time",
               "station_data_state": "available" if station else "missing_or_empty",
               "counts": dict(counts), "sample_count": len(rows),
               "split_counts": dict(Counter(row["split"] for row in rows)),
               "positive_counts": dict(Counter(row["split"] for row in rows if row["label"] == 1)),
               "patch_manifest": str(Path(patch_manifest).resolve()),
               "patch_manifest_sha256": digest(Path(patch_manifest)),
               "station_csv": str(Path(station_csv).resolve()) if station_csv is not None else None,
               "station_csv_sha256": digest(Path(station_csv)) if station_csv is not None and Path(station_csv).is_file() else None,
               "splits": [{"name": name, "start_kst": start.astimezone(KST).isoformat(), "stop_kst_exclusive": stop.astimezone(KST).isoformat()}
                          for name, start, stop in splits],
               "purge_policy": "history_start >= split_start AND target_end < split_stop",
               "note": "No samples are fabricated. Image observation time is not verified operational delivery time."}
    write_json(output / "sample_summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    patches = sub.add_parser("patches", help="Verify originals and extract fixed native DN patches")
    patches.add_argument("--manifest", type=Path, default=ROOT / "logs/gk2a_bronze_manifest.csv")
    patches.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "data/prepared")
    patches.add_argument("--patch-size", type=int, default=64)
    samples = sub.add_parser("samples", help="Join four exact-hour images with next-hour station rain")
    samples.add_argument("--patch-manifest", type=Path, required=True)
    samples.add_argument("--station-csv", type=Path)
    samples.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "data/prepared")
    samples.add_argument("--threshold", type=float, default=30.0)
    samples.add_argument("--split-config", type=Path, help="JSON list of name/start_kst/stop_kst_exclusive")
    args = parser.parse_args(argv)
    if args.command == "patches":
        result = build_patches(args.manifest, args.out, patch_size=args.patch_size)
    else:
        splits = DEFAULT_SPLITS
        if args.split_config:
            values = json.loads(args.split_config.read_text())
            splits = tuple((v['name'],parse_time(v['start_kst']),parse_time(v['stop_kst_exclusive'])) for v in values)
            if [s[0] for s in splits] != ['train','val','test'] or any(s[1]>=s[2] for s in splits) or any(a[2]>b[1] for a,b in zip(splits,splits[1:])):
                raise ValueError('Split configuration must contain ordered, nonoverlapping train/val/test intervals')
        result = build_samples(args.patch_manifest, args.station_csv, args.out, threshold=args.threshold,splits=splits)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
