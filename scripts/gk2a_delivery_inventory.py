#!/usr/bin/env python3
"""Audit existing local GK2A assets and write small, reproducible handoff tables.

No downloads, raster creation, calibration, credentials, or remote storage access.
Uses the existing cropper's native-grid interpretation, not GDAL's identity
transform for the ungeoreferenced NetCDF driver view.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crop_gk2a_silver import grid  # noqa: E402

VERSION = "gk2a-local-handoff-1.0"
KST = timezone(timedelta(hours=9))
SILVER = Path("data/silver/gk2a/IR112_KO")
BRONZE = Path("data/bronze/gk2a/IR112_KO")
MANIFEST = Path("logs/gk2a_bronze_manifest.csv")
NODATA = 65535
COUNT_FIELDS = (
    "outside_pixel_membership_count", "source_nodata_pixel_membership_count",
    "upper_bits_pixel_membership_count", "nonfinite_inside_pixel_membership_count",
    "valid_dn_pixel_membership_count", "invalid_mask_pixel_membership_count",
)


def sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def packed(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def duplicates(rows: list[dict], field: str) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if row.get(field):
            groups[row[field]].append(row["source_path"])
    return [{"value": key, "source_paths": paths} for key, paths in sorted(groups.items()) if len(paths) > 1]


def counts_for(data: np.ndarray, mask: np.ndarray) -> tuple[dict, np.ndarray]:
    """Mutually exclusive mask categories plus explicit nonfinite diagnostics."""
    finite = np.isfinite(data)
    valid = (mask == 1) & finite & (data != NODATA) & (data >= 0) & (data <= 8191)
    return {
        "outside_pixel_membership_count": np.count_nonzero(mask == 0, axis=(1, 2)),
        "source_nodata_pixel_membership_count": np.count_nonzero(mask == 2, axis=(1, 2)),
        "upper_bits_pixel_membership_count": np.count_nonzero(mask == 3, axis=(1, 2)),
        "nonfinite_inside_pixel_membership_count": np.count_nonzero((mask != 0) & ~finite, axis=(1, 2)),
        "valid_dn_pixel_membership_count": np.count_nonzero(valid, axis=(1, 2)),
        "invalid_mask_pixel_membership_count": np.count_nonzero(mask > 3, axis=(1, 2)),
    }, valid


def self_test() -> None:
    data = np.array([[[NODATA, 123, NODATA, 9000, np.nan, 50]]], dtype=float)
    mask = np.array([[[0, 1, 2, 3, 1, 4]]], dtype="uint8")
    counts, valid = counts_for(data, mask)
    assert {key: int(value[0]) for key, value in counts.items()} == dict(zip(COUNT_FIELDS, [1] * 6))
    assert valid.sum() == 1 and data[valid].tolist() == [123]
    unknown = datetime.fromisoformat("2024-11-06T23:58:00+00:00").astimezone(KST)
    assert unknown.isoformat() == "2024-11-07T08:58:00+09:00"
    print("classification and timezone checks passed")


def run(repo: Path, out: Path) -> dict:
    errors = []

    def check(condition, code: str, path: str = "", detail: str = "") -> bool:
        if not condition:
            errors.append({"code": code, "path": path, "detail": detail})
        return bool(condition)

    manifest = read_csv(repo / MANIFEST)
    observations = read_csv(repo / SILVER / "observations.csv")
    crops = read_csv(repo / SILVER / "crop_index.csv")
    processing = json.loads((repo / SILVER / "processing.json").read_text(encoding="utf-8"))
    check(len({row["file_path"] for row in manifest}) == len(manifest), "duplicate_manifest_paths")
    check(len({row["band_index"] for row in observations}) == len(observations), "duplicate_band_indices")
    check(sorted(int(row["band_index"]) for row in observations) == list(range(1, len(observations) + 1)),
          "noncontiguous_band_indices")
    check(len({row["admin_dong_code"] for row in crops}) == len(crops), "duplicate_dong_codes")
    band_by_path = {row["file_path"]: row for row in observations}
    manifest_by_path = {row["file_path"]: row for row in manifest}
    check(set(band_by_path) <= set(manifest_by_path), "band_source_missing_manifest")
    for path, row in band_by_path.items():
        check(all(row.get(key) == value for key, value in manifest_by_path.get(path, {}).items()),
              "band_source_manifest_mismatch", path)
    for key, path in (("processing_code_sha256", Path(processing["processing_code"])),
                      ("bronze_manifest_sha256", Path(processing["bronze_manifest"]))):
        check(sha(repo / path) == processing[key], "processing_provenance_hash_mismatch", path.as_posix())
    boundary = Path(processing["boundary"])
    boundary_sha = sha(repo / boundary)
    check(processing["boundary_version"] == f"sha256:{boundary_sha}", "boundary_hash_mismatch", boundary.as_posix())
    check(processing["calibration"] == "none" and processing["resampling"] == "none", "unexpected_processing")

    inventory = []
    reference_crs = reference_affine = None
    manifest_paths = set(manifest_by_path)
    actual_paths = {path.relative_to(repo).as_posix() for path in (repo / BRONZE).glob("*.nc")}
    check(actual_paths == manifest_paths, "bronze_file_manifest_composition",
          detail=packed({"unlisted": sorted(actual_paths - manifest_paths), "missing": sorted(manifest_paths - actual_paths)}))
    for row in sorted(manifest, key=lambda item: (item["observation_time_utc"], item["file_path"])):
        path = repo / row["file_path"]
        before_errors = len(errors)
        if not check(path.is_file(), "missing_original", row["file_path"]):
            continue
        size = path.stat().st_size
        digest = sha(path)
        hash_matches = check(digest == row["sha256"], "bronze_sha256_mismatch", row["file_path"])
        check(size == int(row["file_size_bytes"]), "bronze_size_mismatch", row["file_path"])
        with rasterio.open(path) as source:
            crs, affine = grid(source)
            if reference_crs is None:
                reference_crs, reference_affine = crs, affine
            check(crs == reference_crs and affine == reference_affine, "inconsistent_native_grid", row["file_path"])
            tags, band = source.tags(), source.tags(1)
            product = tags.get("NC_GLOBAL#file_name", "")
            match = re.search(r"_(\d{12})\.nc$", product)
            # A declared product filename is the available acquisition-time evidence.
            # Do not substitute filesystem times or a nominal daily 09:00 KST time.
            actual_utc = datetime.strptime(match[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc) if match else None
            check(actual_utc is not None, "unknown_observation_time", row["file_path"])
            utc = actual_utc.isoformat() if actual_utc else ""
            kst = actual_utc.astimezone(KST).isoformat() if actual_utc else ""
            check(utc == row["observation_time_utc"] and kst == row["observation_time_kst"],
                  "timestamp_provenance_mismatch", row["file_path"])
            check(not actual_utc or actual_utc.astimezone(KST).date().isoformat() == row["date_kst"],
                  "kst_date_mismatch", row["file_path"])
            bounds = list(array_bounds(source.height, source.width, affine))
            wgs84_bounds = list(transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21))
            inventory.append({
                "source_path": row["file_path"], "product_filename": product,
                "original_source_product": tags.get("NC_GLOBAL#origianl_sourece_file", ""),
                "satellite": tags.get("NC_GLOBAL#satellite_name", ""),
                "sensor": tags.get("NC_GLOBAL#instrument_name", ""),
                "level": "LE1B", "channel": band.get("channel_name", ""),
                "center_wavelength_um": tags.get("NC_GLOBAL#channel_center_wavelength", ""),
                "source_variable": band.get("NETCDF_VARNAME", ""),
                "observation_time_utc": utc, "observation_time_kst": kst,
                "date_kst": actual_utc.astimezone(KST).date().isoformat() if actual_utc else "",
                "timestamp_basis": "NetCDF global file_name; checked against normalized manifest",
                "daily_representative": row["daily_representative"],
                "silver_band_index": band_by_path.get(row["file_path"], {}).get("band_index", ""),
                "source_endpoint": row["source_endpoint"], "request_date_utc": row["request_date_utc"],
                "download_time_utc": "", "download_time_status": "not established by normalized provenance",
                "file_size_bytes": size, "sha256": digest, "sha256_matches_manifest": hash_matches,
                "checksum_check_level": "fresh full-byte SHA-256",
                "height": source.height, "width": source.width, "band_count": source.count,
                "dtype": source.dtypes[0], "source_nodata": source.nodata,
                "valid_bits": band.get("number_of_valid_bits_per_pixel", ""),
                "stored_bits": band.get("number_of_total_bits_per_pixel", ""),
                "source_gdal_crs_present": source.crs is not None,
                "native_crs_wkt": crs.to_wkt(), "native_transform": packed(list(affine)[:6]),
                "georeference_basis": "reconstructed from NetCDF projection and pixel-center tags by existing cropper",
                "pixel_width_m": affine.a, "pixel_height_m": abs(affine.e),
                "native_bounds_m": packed(bounds), "wgs84_envelope_lonlat": packed(wgs84_bounds),
                "units": "packed_DN", "scale": source.scales[0], "offset": source.offsets[0],
                "metadata_min_dn": band.get("min_pixel_value", ""),
                "metadata_max_dn": band.get("max_pixel_value", ""),
                "metadata_average_dn": band.get("average_pixel_value", ""),
                "quality_status": "local_integrity_metadata_pass" if len(errors) == before_errors else "local_audit_failed",
                "source_full_scene_pixel_qc": "not recomputed; metadata statistics only",
                "cloud_qc_status": "unverified", "calibration_status": "not converted to temperature",
            })

    n = len(observations)
    totals = {key: np.zeros(n, dtype=np.int64) for key in COUNT_FIELDS}
    inside_memberships = np.zeros(n, dtype=np.int64)
    sums = np.zeros(n, dtype=np.float64)
    min_dn = np.full(n, np.inf)
    max_dn = np.full(n, -np.inf)
    expected_qc = {}
    qc_path = SILVER / "observation_qc.csv.gz"
    with gzip.open(repo / qc_path, "rt", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["admin_dong_code"], int(row["band_index"]))
            check(key not in expected_qc, "duplicate_existing_qc_key", detail=packed(key))
            expected_qc[key] = tuple(int(row[name]) for name in
                                     ("valid_pixel_count", "source_nodata_count", "upper_bits_set_count"))
    spatial_rows = []
    raster_bytes = 0
    missing_dong_bands = np.zeros(n, dtype=np.int64)
    observed_mask_codes = Counter()
    raster_paths = set()
    for crop in sorted(crops, key=lambda row: row["admin_dong_code"]):
        code = crop["admin_dong_code"]
        image_rel = SILVER / crop["image_path"]
        mask_rel = SILVER / crop["quality_mask_path"]
        raster_paths.update((image_rel.as_posix(), mask_rel.as_posix()))
        image_sha, mask_sha = sha(repo / image_rel), sha(repo / mask_rel)
        check(image_sha == crop["image_sha256"], "silver_sha256_mismatch", image_rel.as_posix())
        check(mask_sha == crop["quality_mask_sha256"], "mask_sha256_mismatch", mask_rel.as_posix())
        raster_bytes += (repo / image_rel).stat().st_size + (repo / mask_rel).stat().st_size
        with rasterio.open(repo / image_rel) as image, rasterio.open(repo / mask_rel) as mask:
            check(image.shape == mask.shape == (int(crop["height"]), int(crop["width"])), "raster_shape_mismatch", code)
            check(image.count == mask.count == n, "raster_band_count_mismatch", code)
            check(image.crs == mask.crs == reference_crs, "silver_crs_mismatch", code)
            expected_affine = reference_affine * rasterio.Affine.translation(int(crop["source_col"]), int(crop["source_row"]))
            check(image.transform == mask.transform == expected_affine, "silver_transform_mismatch", code)
            check(image.nodata == NODATA and mask.nodata is None, "silver_nodata_metadata_mismatch", code)
            check(set(image.dtypes) == {"uint16"} and set(mask.dtypes) == {"uint8"}, "silver_dtype_mismatch", code)
            check(image.tags().get("units") == "packed_DN" and image.tags().get("channel") == "IR112",
                  "silver_units_or_channel_mismatch", code)
            available = min(image.count, mask.count, n)
            missing_dong_bands[available:] += 1
            data, quality = image.read(list(range(1, available + 1))), mask.read(list(range(1, available + 1)))
            counts, valid = counts_for(data, quality)
            observed_mask_codes.update({str(int(value)): int(count) for value, count in
                                        zip(*np.unique(quality, return_counts=True))})
            check(not np.any(quality > 3), "unknown_mask_code", code)
            check(np.all(data[(quality == 0) | (quality == 2)] == NODATA), "outside_or_source_nodata_not_filled", code)
            check(np.all((data[quality == 1] <= 8191) & np.isfinite(data[quality == 1])), "invalid_valid_dn", code)
            check(np.all((data[quality == 3] > 8191) & (data[quality == 3] != NODATA)), "invalid_upper_bits_dn", code)
            check(np.all(quality == 0, axis=0).sum() == int(crop["height"]) * int(crop["width"]) - int(crop["geometry_pixel_count"]),
                  "outside_geometry_count_mismatch", code)
            check(np.all((quality == 0) == (quality[0] == 0)), "outside_mask_changes_across_bands", code)
            for key, values in counts.items():
                totals[key][:available] += values
            inside_memberships[:available] += np.count_nonzero(quality != 0, axis=(1, 2))
            sums[:available] += np.where(valid, data, 0).sum(axis=(1, 2), dtype=np.float64)
            min_dn[:available] = np.minimum(min_dn[:available], np.where(valid, data, np.inf).min(axis=(1, 2)))
            max_dn[:available] = np.maximum(max_dn[:available], np.where(valid, data, -np.inf).max(axis=(1, 2)))
            for index in range(available):
                actual = tuple(int(counts[key][index]) for key in ("valid_dn_pixel_membership_count",
                                     "source_nodata_pixel_membership_count", "upper_bits_pixel_membership_count"))
                check(expected_qc.pop((code, index + 1), None) == actual, "existing_qc_mismatch", code, str(index + 1))
            spatial_rows.append({
                "admin_dong_code": code, "admin_dong_name": crop["admin_dong_name"],
                "image_path": image_rel.as_posix(), "quality_mask_path": mask_rel.as_posix(),
                "image_sha256": image_sha, "quality_mask_sha256": mask_sha,
                "band_count": image.count, "height": image.height, "width": image.width,
                "native_crs_wkt": image.crs.to_wkt(), "native_transform": packed(list(image.transform)[:6]),
                "pixel_width_m": image.res[0], "pixel_height_m": image.res[1],
                "native_bounds_m": packed(list(image.bounds)),
                "wgs84_envelope_lonlat": packed(list(transform_bounds(image.crs, "EPSG:4326", *image.bounds, densify_pts=21))),
                "source_row": crop["source_row"], "source_col": crop["source_col"],
                "geometry_pixel_count": crop["geometry_pixel_count"], "image_nodata": image.nodata,
                "units": image.tags().get("units", ""), "boundary_path": boundary.as_posix(),
                "boundary_sha256": boundary_sha, "boundary_validity_period": "unknown",
                "geometry_rule": "all_touched; native pixels can be shared by multiple dongs",
            })
    actual_silver_paths = {path.relative_to(repo).as_posix() for path in (repo / SILVER).glob("*/*.tif")}
    check(actual_silver_paths == raster_paths, "silver_file_manifest_composition")
    check(not expected_qc, "unmatched_existing_qc_rows", detail=str(len(expected_qc)))

    cleaned = []
    for original in inventory:
        band_index = original["silver_band_index"]
        index = int(band_index) - 1 if band_index else None
        count_values = {key: int(totals[key][index]) if index is not None else "" for key in COUNT_FIELDS}
        valid_count = count_values["valid_dn_pixel_membership_count"]
        inside_count = int(inside_memberships[index]) if index is not None else None
        cleaned.append({
            "source_path": original["source_path"], "source_sha256": original["sha256"],
            "observation_time_utc": original["observation_time_utc"],
            "observation_time_kst": original["observation_time_kst"], "date_kst": original["date_kst"],
            "daily_representative": original["daily_representative"], "silver_band_index": band_index,
            "silver_status": "existing_selected_band" if band_index else "bronze_only_not_selected_for_silver",
            "expected_dong_count": len(crops),
            "available_dong_count": len(crops) - int(missing_dong_bands[index]) if index is not None else 0,
            "missing_silver_dong_band_count": int(missing_dong_bands[index]) if index is not None else len(crops),
            **count_values,
            "inside_pixel_membership_count": inside_count if inside_count is not None else "",
            "valid_dn_membership_fraction": valid_count / inside_count if inside_count else "",
            "dn_min": int(min_dn[index]) if index is not None and valid_count else "",
            "dn_max": int(max_dn[index]) if index is not None and valid_count else "",
            "dn_mean_over_dong_memberships": float(sums[index] / valid_count) if index is not None and valid_count else "",
            "units": "packed_DN", "aggregation_basis": "dong-pixel memberships; shared source pixels repeated",
            "temperature_k": "", "cloud_fraction": "", "cloud_qc_status": "unverified",
            "missing_value_rule": "empty numeric cell means unavailable; zero means measured zero count",
        })
    selected = [row for row in cleaned if row["silver_band_index"]]
    dates = sorted(row["date_kst"] for row in selected)
    expected_dates = []
    if dates:
        current, end = datetime.fromisoformat(dates[0]), datetime.fromisoformat(dates[-1])
        while current <= end:
            expected_dates.append(current.date().isoformat())
            current += timedelta(days=1)
    check(dates == expected_dates, "daily_kst_date_gap_or_duplicate")
    duplicate_checks = {key: duplicates(inventory, key) for key in ("source_path", "sha256", "product_filename", "observation_time_utc")}
    check(all(not value for value in duplicate_checks.values()), "duplicate_original_identity_or_bytes")
    col0 = min(int(row["source_col"]) for row in crops)
    row0 = min(int(row["source_row"]) for row in crops)
    col1 = max(int(row["source_col"]) + int(row["width"]) for row in crops)
    row1 = max(int(row["source_row"]) + int(row["height"]) for row in crops)
    input_paths = [MANIFEST, SILVER / "observations.csv", SILVER / "crop_index.csv", SILVER / "processing.json", qc_path, boundary,
                   Path(processing["processing_code"]), Path("register_crop.py"), Path("scripts/gk2a_delivery_inventory.py")]
    summary = {
        "report_version": VERSION, "scope": "existing local main-project GK2A; no collection or raster regeneration",
        "software_versions": {"python": sys.version.split()[0], "numpy": np.__version__,
                              "rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__},
        "status": "pass" if not errors else "failed", "errors": errors,
        "inventory_counts": {"originals": len(inventory), "selected_daily_observations": len(observations),
            "bronze_only_extras": len(inventory) - len(observations), "dongs": len(crops),
            "silver_and_mask_files": len(raster_paths), "existing_dong_observation_qc_rows_checked": len(crops) * n,
            "original_bytes_checked": sum(row["file_size_bytes"] for row in inventory), "silver_and_mask_bytes_checked": raster_bytes},
        "checks": {"checksum": "fresh full-byte SHA-256 of every inventoried original, image, and mask",
            "source_raster": "every original opened; channel, datatype, bits, native-grid tags, time and manifest checked; full-scene pixels not rescanned",
            "silver_content": "every image and mask band read; codes, outside fill, nodata, finite DN, upper bits, shape, CRS, transform and existing QC compared",
            "existing_qc_path": qc_path.as_posix(), "no_rasters_rebuilt": True,
            "inputs_sha256": {path.as_posix(): sha(repo / path) for path in input_paths}},
        "temporal": {"selected_kst_date_range": [dates[0], dates[-1]], "selected_date_gaps": sorted(set(expected_dates) - set(dates)),
            "actual_selected_utc_clock_counts": dict(sorted(Counter(row["observation_time_utc"][11:16] for row in selected).items())),
            "selected_observations_away_from_0900_kst": [{key: row[key] for key in ("source_path", "observation_time_utc", "observation_time_kst", "date_kst")}
                for row in selected if row["observation_time_kst"][11:16] != "09:00"],
            "sampling": "selected one-per-KST-day archive, with five additional originals; not a complete two-minute series",
            "unknown_download_times": len(inventory)},
        "spatial": {"pixel_size_m": 2000, "resampling": "none", "seoul_union_native_window_rows": row1 - row0,
            "seoul_union_native_window_columns": col1 - col0, "seoul_union_source_row": row0, "seoul_union_source_col": col0,
            "boundary_path": boundary.as_posix(), "boundary_sha256": boundary_sha, "boundary_validity_period": None,
            "geometry_rule": "all_touched; counts are dong memberships, not unique source pixels or ground-area coverage"},
        "quality": {"mask_code_counts_over_dong_band_memberships": {str(code): observed_mask_codes[str(code)] for code in range(4)},
            "inside_pixel_membership_count": int(inside_memberships.sum()),
            **{key: int(value.sum()) for key, value in totals.items()},
            "selected_missing_dong_band_count": int(missing_dong_bands.sum()),
            "bronze_only_missing_dong_band_count": sum(row["missing_silver_dong_band_count"] for row in cleaned if not row["silver_band_index"]),
            "dn_min": min(row["dn_min"] for row in selected if row["dn_min"] != ""),
            "dn_max": max(row["dn_max"] for row in selected if row["dn_max"] != ""),
            "units": "packed_DN", "valid_numeric_rule": "mask==1 and finite and 0<=DN<=8191; exclude outside, nodata and upper bits",
            "cloud_qc_status": "unverified", "cloud_fraction": None, "temperature_calibration": "none",
            "precipitation_label_status": "not joined", "ground_area_coverage": None},
        "duplicates": duplicate_checks,
        "unit_checks": {"source_valid_bits": 13, "stored_bits": 16, "source_scale": 1.0, "source_offset": 0.0,
            "silver_tag": "packed_DN", "kelvin_conversion": "not available in this handoff", "all_native_resolutions_m": [2000]},
        "prior_audit_reference": "reports/2026-11-23-verification/local-assets.json",
    }
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "original_inventory.csv", inventory)
    write_csv(out / "cleaned_observations.csv", cleaned)
    write_csv(out / "silver_raster_inventory.csv", spatial_rows)
    (out / "quality_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, default=Path("reports/2026-11-26-data-handoff/gk2a"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    repo = args.repo_root.resolve()
    out = args.out if args.out.is_absolute() else repo / args.out
    warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
    with rasterio.Env(GDAL_NETCDF_BOTTOMUP="NO"):
        summary = run(repo, out)
    print(json.dumps({"status": summary["status"], **summary["inventory_counts"], "errors": len(summary["errors"])}, ensure_ascii=False))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
