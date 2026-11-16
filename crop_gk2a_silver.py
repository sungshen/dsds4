"""Crop daily GK2A IR112 observations to Seoul administrative dongs on the native grid."""
import argparse
import csv
import gzip
import json
import warnings
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import bounds, geometry_mask
from rasterio.transform import from_origin, xy
from rasterio.warp import transform, transform_geom
from rasterio.windows import from_bounds
from rasterio.windows import transform as window_transform

from register_crop import sha

VERSION = "gk2a-ir112-ko-silver-1.0"
REFERENCE_URL = "https://nmsc.kma.go.kr/upload/resource/data/gk2a/gk2a_ami_ko020lc_latlon.nc"
NODATA = 65535


def grid(dataset):
    tags = {
        key.removeprefix("NC_GLOBAL#"): value
        for key, value in dataset.tags().items() if key.startswith("NC_GLOBAL#")
    }
    band = dataset.tags(1)
    if (
        tags["projection_type"] != "lambert_conformal_conic"
        or tags["satellite_name"] != "GK-2A" or tags["instrument_name"] != "AMI"
        or band["NETCDF_VARNAME"] != "image_pixel_values" or band["channel_name"] != "IR112"
        or dataset.count != 1 or dataset.dtypes != ("uint16",)
        or band["number_of_valid_bits_per_pixel"] != "13"
        or band["number_of_total_bits_per_pixel"] != "16"
        or dataset.scales != (1.0,) or dataset.offsets != (0.0,)
    ):
        raise ValueError(f"Unexpected source raster: {dataset.name}")
    crs = CRS.from_dict(
        proj="lcc", datum="WGS84", units="m",
        lat_0=float(tags["origin_latitude"]), lon_0=float(tags["central_meridian"]),
        lat_1=float(tags["standard_parallel1"]), lat_2=float(tags["standard_parallel2"]),
        x_0=float(tags["false_easting"]), y_0=float(tags["false_northing"]),
    )
    step = float(tags["pixel_size"])
    left = float(tags["upper_left_easting"])
    top = float(tags["upper_left_northing"])
    if (
        step != 2000 or dataset.shape != (900, 900)
        or left + (dataset.width - 1) * step != float(tags["lower_right_easting"])
        or top - (dataset.height - 1) * step != float(tags["lower_right_northing"])
    ):
        raise ValueError(f"Unexpected KO grid dimensions: {dataset.name}")
    return crs, from_origin(left - step / 2, top + step / 2, step, step)


def verify_reference(path, crs, affine):
    rows, columns = np.meshgrid([0, 100, 449, 472, 479, 500, 899], [0, 449, 490, 500, 899])
    rows, columns = rows.ravel(), columns.ravel()
    xs, ys = xy(affine, rows, columns)
    expected = transform(crs, "EPSG:4326", xs, ys)
    errors = []
    for axis, predicted in zip(("lon", "lat"), expected):
        with rasterio.open(f'NETCDF:"{path.resolve()}":{axis}') as src:
            actual = src.read(1)[rows, columns]
        errors.append(float(np.max(np.abs(actual - predicted))))
    if not all(error < 0.00002 for error in errors):
        raise ValueError(f"KO grid does not match KMA pixel centers: {errors}")
    return {"url": REFERENCE_URL, "sha256": sha(path),
            "checked_pixel_centers": len(rows), "max_error_degrees": max(errors)}


def get_dongs(boundary, src, crs, affine):
    geo = json.loads(boundary.read_text(encoding="utf-8"))
    declared = geo.get("crs", {}).get("properties", {}).get("name", "CRS84")
    if geo["type"] != "FeatureCollection" or not ("CRS84" in declared or "4326" in declared):
        raise ValueError("Expected a WGS84 boundary FeatureCollection")
    # The NetCDF driver does not expose the native affine.
    grid_bounds = rasterio.transform.array_bounds(src.height, src.width, affine)
    if not geo["features"]:
        raise ValueError("No boundary features")
    dongs = {}
    for feature in geo["features"]:
        props = feature["properties"]
        code = str(props["adm_cd2"])
        if len(code) != 10 or not code.isascii() or not code.isdigit() or not code.startswith("11") or code in dongs:
            raise ValueError(f"Expected a unique Seoul administrative code: {code}")
        geom = transform_geom("EPSG:4326", crs, feature["geometry"])
        left, bottom, right, top = bounds(geom)
        if not (grid_bounds[0] <= left < right <= grid_bounds[2]
                and grid_bounds[1] <= bottom < top <= grid_bounds[3]):
            raise ValueError(f"Out-of-grid boundary: {code}")
        window = from_bounds(left, bottom, right, top, affine)
        # Round outwards so every intersected native pixel is available.
        col0, row0 = np.floor([window.col_off, window.row_off]).astype(int)
        col1, row1 = np.ceil([window.col_off + window.width,
                              window.row_off + window.height]).astype(int)
        window = rasterio.windows.Window(col0, row0, col1 - col0, row1 - row0)
        crop_affine = window_transform(window, affine)
        inside = geometry_mask([geom], (row1-row0, col1-col0), crop_affine,
                               all_touched=True, invert=True)
        if not inside.any():
            raise ValueError(f"Empty polygon mask: {code}")
        dongs[code] = (props["adm_nm"], window, crop_affine, inside)
    return dongs


def write_csv(stream, rows):
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)


def process(manifest, boundary, output, reference):
    with manifest.open(encoding="utf-8-sig", newline="") as stream:
        observations = sorted(
            (row for row in csv.DictReader(stream) if row["daily_representative"] == "True"),
            key=lambda row: row["date_kst"],
        )
    dates = [row["date_kst"] for row in observations]
    if not dates or len(set(dates)) != len(dates):
        raise ValueError("Expected one representative observation per date")
    with rasterio.open(observations[0]["file_path"]) as first:
        crs, affine = grid(first)
        dongs = get_dongs(boundary, first, crs, affine)
    reference_check = verify_reference(reference, crs, affine) if reference else None

    windows = [entry[1] for entry in dongs.values()]
    col0 = min(w.col_off for w in windows)
    row0 = min(w.row_off for w in windows)
    col1 = max(w.col_off + w.width for w in windows)
    row1 = max(w.row_off + w.height for w in windows)
    city_window = rasterio.windows.Window(col0, row0, col1 - col0, row1 - row0)
    cube = np.empty((len(observations), row1 - row0, col1 - col0), dtype="uint16")
    missing = np.empty(cube.shape, dtype=bool)
    obs_index = []
    for i, observation in enumerate(observations):
        path = Path(observation["file_path"])
        if sha(path) != observation["sha256"]:
            raise ValueError(f"Bronze checksum mismatch: {path}")
        with rasterio.open(path) as src:
            timestamp = datetime.fromisoformat(observation["observation_time_utc"])
            if (grid(src) != (crs, affine)
                    or not src.tags()["NC_GLOBAL#file_name"].endswith(timestamp.strftime("_%Y%m%d%H%M.nc"))):
                raise ValueError(f"Observation or grid mismatch: {path}")
            tile = src.read(1, window=city_window, masked=True)
            cube[i] = tile.data
            missing[i] = np.ma.getmaskarray(tile)
        obs_index.append({"band_index": i + 1, **observation})

    crop_index = []
    qc_fields = ["admin_dong_code", "band_index", "valid_pixel_count",
                 "source_nodata_count", "upper_bits_set_count"]
    with gzip.open(output / "observation_qc.csv.gz", "wt", encoding="utf-8", newline="") as stream:
        qc = csv.writer(stream)
        qc.writerow(qc_fields)
        for code, (name, window, crop_affine, inside) in sorted(dongs.items()):
            r = window.row_off - row0
            c = window.col_off - col0
            raw = cube[:, r:r + window.height, c:c + window.width]
            absent = missing[:, r:r + window.height, c:c + window.width]
            upper_bits = raw > 8191
            quality = np.where(~inside, 0, np.where(absent, 2, np.where(upper_bits, 3, 1))).astype("uint8")
            pixels = np.where((quality == 0) | (quality == 2), NODATA, raw).astype("uint16")
            profile = dict(driver="GTiff", count=len(observations), crs=crs, transform=crop_affine,
                           width=window.width, height=window.height, compress="deflate")
            folder = output / code
            folder.mkdir()
            image_path = folder / "admin_crop.tif"
            mask_path = folder / "quality_mask.tif"
            with rasterio.open(image_path, "w", **profile, dtype="uint16", nodata=NODATA) as dst:
                dst.write(pixels)
                dst.update_tags(admin_dong_code=code, channel="IR112", units="packed_DN",
                                time_band_index="../observations.csv", processing_version=VERSION)
            with rasterio.open(mask_path, "w", **profile, dtype="uint8") as dst:
                dst.write(quality)
                dst.update_tags(codes="0=outside; 1=valid DN; 2=source nodata; 3=upper bits set",
                                processing_version=VERSION)
            for path, expected in ((image_path, pixels), (mask_path, quality)):
                with rasterio.open(path) as saved:
                    if saved.crs != crs or saved.transform != crop_affine or not np.array_equal(saved.read(), expected):
                        raise ValueError(f"Silver raster verification failed: {path}")
            qc.writerows(zip([code] * len(observations), range(1, len(observations) + 1),
                             *(np.count_nonzero(quality == val, axis=(1, 2)) for val in (1, 2, 3))))
            crop_index.append({
                "admin_dong_code": code, "admin_dong_name": name,
                "image_path": f"{code}/{image_path.name}", "quality_mask_path": f"{code}/{mask_path.name}",
                "height": window.height, "width": window.width, "geometry_pixel_count": int(inside.sum()),
                "source_row": window.row_off, "source_col": window.col_off,
                "image_sha256": sha(image_path), "quality_mask_sha256": sha(mask_path),
            })

    for filename, rows in (("observations.csv", obs_index), ("crop_index.csv", crop_index)):
        with (output / filename).open("w", encoding="utf-8", newline="") as stream:
            write_csv(stream, rows)
    metadata = {
        "processing_code": Path(__file__).name, "processing_version": VERSION,
        "processing_code_sha256": sha(__file__),
        "bronze_manifest": str(manifest), "bronze_manifest_sha256": sha(manifest),
        "boundary": str(boundary), "boundary_version": f"sha256:{sha(boundary)}",
        "boundary_validity_period": None, "admin_code_field": "adm_cd2",
        "crs_wkt": crs.to_wkt(), "native_transform": list(affine)[:6], "pixel_size_m": 2000,
        "position_reference_check": reference_check, "source_band": "IR112",
        "source_variable": "image_pixel_values", "units": "packed digital number (DN)",
        "calibration": "none", "resampling": "none", "image_nodata": NODATA,
        "geometry_mask": "all_touched; boundary pixels can be shared across dongs",
        "quality_mask_codes": {"0": "outside dong", "1": "valid 13-bit DN", "2": "source nodata",
                               "3": "upper bits set; raw code preserved; excluded from valid count"},
        "cloud_qc_status": "unverified", "cloud_fraction": None,
        "observation_count": len(observations), "dong_count": len(dongs),
        "observation_dong_count": len(observations) * len(dongs),
    }
    (output / "processing.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    print(f"{len(observations)} observations × {len(dongs)} dongs verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("logs/gk2a_bronze_manifest.csv"))
    parser.add_argument("--boundary", type=Path, default=Path("config/hangjeongdong_서울특별시.geojson"))
    parser.add_argument("--out", type=Path, default=Path("data/silver/gk2a/IR112_KO"))
    parser.add_argument("--reference-grid", type=Path, help=f"Optional official reference grid: {REFERENCE_URL}")
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f"Output already exists: {args.out}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
    with TemporaryDirectory(dir=args.out.parent, prefix=".silver-") as temporary:
        staged = Path(temporary) / "output"
        staged.mkdir()
        # Raw KO NetCDF arrays are north-first, matching the KMA latitude grid.
        with rasterio.Env(GDAL_NETCDF_BOTTOMUP="NO"):
            process(args.manifest, args.boundary, staged, args.reference_grid)
        staged.rename(args.out)


if __name__ == "__main__":
    main()
