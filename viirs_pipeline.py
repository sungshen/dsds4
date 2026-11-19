"""Download Suomi NPP VIIRS swaths and extract native pixel centers by dong."""

import argparse
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
BRONZE = ROOT / "data/bronze/viirs"
SILVER = ROOT / "data/silver/viirs"
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
DOWNLOAD_HOST = "data.laadsdaac.earthdatacloud.nasa.gov"
PRODUCTS = ("VNP02IMG", "VNP03IMG")
KST = timezone(timedelta(hours=9))
BBOX = (124, 32, 132, 39)
FILENAME = re.compile(r"(VNP0[23]IMG)\.(A\d{7}\.\d{4})\.002\.\d{13}\.nc")


def timestamp(key):
    return datetime.strptime(key, "A%Y%j.%H%M").replace(tzinfo=timezone.utc)


def download_link(entry, product, name):
    cloud_path = f"/prod-lads/{product}/{name}"
    s3_url = f"s3://prod-lads/{product}/{name}"
    for link in entry.get("links", []):
        url = link.get("href", "")
        parsed = urlsplit(url)
        if (link.get("rel", "").endswith("/data#") and parsed.scheme == "https"
                and parsed.netloc == DOWNLOAD_HOST and parsed.path == cloud_path and not parsed.query):
            return url
    if any(link.get("href") == s3_url for link in entry.get("links", [])):
        return f"https://{DOWNLOAD_HOST}{cloud_path}"
    raise ValueError(f"No NASA cloud download link for {name}")


def search(session, product, start, stop):
    params = {
        "provider": "LAADS", "short_name": product, "version": "2",
        "bounding_box": ",".join(map(str, BBOX)),
        "temporal": f"{start.isoformat()},{stop.isoformat()}",
        "page_size": 2000, "sort_key": "start_date",
    }
    headers = {}
    files = {}
    while True:
        response = session.get(CMR, params=params, headers=headers, timeout=60)
        response.raise_for_status()
        entries = response.json()["feed"]["entry"]
        if not entries:
            return files
        for entry in entries:
            name = entry["producer_granule_id"]
            match = FILENAME.fullmatch(name)
            if not match or match[1] != product:
                raise ValueError(f"Unexpected CMR granule name: {name}")
            key = match[2]
            if start <= timestamp(key) < stop and (key not in files or name > files[key][0]):
                files[key] = (name, download_link(entry, product, name))
        cursor = response.headers.get("CMR-Search-After")
        if not cursor:
            if len(entries) == params["page_size"]:
                raise RuntimeError("CMR omitted the pagination cursor")
            return files
        if cursor == headers.get("CMR-Search-After"):
            raise RuntimeError("CMR pagination cursor did not advance")
        headers["CMR-Search-After"] = cursor


def download(session, root, key, asset, token):
    from netCDF4 import Dataset

    name, url = asset
    observed = timestamp(key)
    path = root / observed.astimezone(KST).strftime("%Y/%m/%d") / name
    if path.exists():
        with Dataset(path):
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as work:
        temporary = Path(work) / name
        try:
            with session.get(url, headers={"Authorization": f"Bearer {token}"},
                             stream=True, timeout=(30, 120)) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"{name}: NASA download returned HTTP {response.status_code}")
                if "html" in response.headers.get("Content-Type", "").lower():
                    raise RuntimeError(f"{name}: NASA returned a sign-in page instead of NetCDF")
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
        except requests.RequestException as error:
            raise RuntimeError(f"{name}: NASA transfer failed ({type(error).__name__})") from None
        with Dataset(temporary):
            pass
        os.replace(temporary, path)
    return path


def configured_value(name):
    value = os.environ.get(name)
    if value:
        return value
    config = ROOT / ".env"
    if config.is_file():
        for line in config.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def earthdata_token():
    return configured_value("EARTHDATA_TOKEN")


def bronze(args):
    token = None if args.inventory else earthdata_token()
    if not args.inventory and not token:
        raise SystemExit("Set EARTHDATA_TOKEN in the environment or the repository's .env file.")
    with requests.Session() as session:
        retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        start = args.start
        while start < args.stop:
            stop = min(start + timedelta(days=30), args.stop)
            utc_start = datetime.combine(start, time.min, KST).astimezone(timezone.utc)
            utc_stop = datetime.combine(stop, time.min, KST).astimezone(timezone.utc)
            imagery, geo = (search(session, product, utc_start, utc_stop) for product in PRODUCTS)
            keys = imagery.keys() & geo.keys()
            missing = imagery.keys() ^ geo.keys()
            print(f"{start} to {stop} (KST): {len(keys)} pairs, {len(missing)} unpaired", flush=True)
            for key in sorted(missing):
                print(f"Unpaired: {key}", flush=True)
            if not args.inventory:
                for key in sorted(keys):
                    download(session, args.bronze_dir, key, imagery[key], token)
                    download(session, args.bronze_dir, key, geo[key], token)
            start = stop


def boundaries(path, column):
    import geopandas as gpd

    frame = gpd.read_file(path)
    if frame.crs is None or frame.empty or frame[column].isna().any():
        raise ValueError("Boundaries require a CRS, geometries and administrative codes")
    frame = frame[[column, frame.geometry.name]].rename(columns={column: "dong_code"})
    frame["dong_code"] = frame["dong_code"].astype(str).str.strip()
    if not frame["dong_code"].str.fullmatch(r"[0-9]+").all():
        raise ValueError("Expected numeric administrative codes")
    frame = frame.to_crs(4326)
    if (not frame.geom_type.isin(["Polygon", "MultiPolygon"]).all()
            or frame.is_empty.any() or not frame.is_valid.all()):
        raise ValueError("Boundaries must contain valid non-empty polygons")
    left, bottom, right, top = frame.total_bounds
    if not (BBOX[0] <= left < 126 < 130 < right <= BBOX[2]
            and BBOX[1] <= bottom < 34 < 38 < top <= BBOX[3]):
        raise ValueError("Expected nationwide boundaries including the offshore islands")
    return frame.dissolve("dong_code").sort_index()


def extract(image_path, geo_path, dongs, writer):
    import geopandas as gpd
    import numpy as np
    import pyarrow as pa
    from netCDF4 import Dataset

    left, bottom, right, top = dongs.total_bounds
    with Dataset(image_path) as image, Dataset(geo_path) as geo:
        data = image["observation_data"]
        band = data["I05"]
        band.set_auto_scale(False)
        quality = data["I05_quality_flags"]
        quality.set_auto_maskandscale(False)
        lookup = data["I05_brightness_temperature_lut"][:].astype("float32").filled(np.nan)
        lon = geo["geolocation_data/longitude"]
        lat = geo["geolocation_data/latitude"]
        if band.shape != lon.shape or lon.shape != lat.shape or quality.shape != band.shape:
            raise ValueError(f"Inconsistent swath dimensions: {image_path.name}")
        for first in range(0, band.shape[0], 256):
            rows = slice(first, first + 256)
            x = lon[rows].astype("float64").filled(np.nan)
            y = lat[rows].astype("float64").filled(np.nan)
            row, col = np.nonzero((left <= x) & (x <= right) & (bottom <= y) & (y <= top))
            if not len(row):
                continue
            points = gpd.points_from_xy(x[row, col], y[row, col])
            pixel, dong = dongs.sindex.query(points, predicate="within")
            if not len(pixel):
                continue
            if np.unique(pixel).size != pixel.size:
                raise ValueError("Administrative polygons overlap at a pixel center")
            row, col = row[pixel], col[pixel]
            dn = band[rows][row, col]
            raw = np.ma.getdata(dn)
            valid = ~np.ma.getmaskarray(dn) & (raw >= 0) & (raw < lookup.size)
            temperature = np.full(len(row), np.nan, dtype="float32")
            temperature[valid] = lookup[raw[valid]]
            writer.write_table(pa.Table.from_arrays([
                pa.array(dongs.index.to_numpy()[dong], type=pa.string()),
                pa.array(row + first, type=pa.int32()), pa.array(col, type=pa.int32()),
                pa.array(x[row, col]), pa.array(y[row, col]),
                pa.array(temperature, mask=~np.isfinite(temperature)),
                pa.array(quality[rows][row, col], type=pa.uint16()),
            ], schema=writer.schema))


def write_crop(destination, key, pair, dongs, boundary, boundary_version):
    import pyarrow as pa
    import pyarrow.parquet as pq

    observed = timestamp(key)
    schema = pa.schema([
        ("dong_code", pa.string()), ("row", pa.int32()), ("column", pa.int32()),
        ("longitude", pa.float64()), ("latitude", pa.float64()),
        ("i05_brightness_temperature_k", pa.float32()), ("i05_quality_flags", pa.uint16()),
    ], metadata={
        "swath_start_utc": observed.isoformat(),
        "boundary": str(boundary),
        "boundary_version": boundary_version,
        "sources": json.dumps([pair[product].name for product in PRODUCTS]),
    })
    if destination.exists() and pq.read_schema(destination).equals(schema, check_metadata=True):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as work:
        temporary = Path(work) / destination.name
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            extract(pair[PRODUCTS[0]], pair[PRODUCTS[1]], dongs, writer)
        os.replace(temporary, destination)


def silver(args):
    dongs = boundaries(args.boundary, args.code_column)
    start = max(args.start, date(args.year, 1, 1))
    stop = min(args.stop, date(args.year + 1, 1, 1))
    pairs = {}
    roots = args.bronze_dir or [BRONZE]
    for root in roots:
        for path in root.glob(f"{args.year}/*/*/*.nc"):
            match = FILENAME.fullmatch(path.name)
            if match and start <= timestamp(match[2]).astimezone(KST).date() < stop:
                by_product = pairs.setdefault(match[2], {})
                previous = by_product.get(match[1])
                if previous is None or path.name > previous.name:
                    by_product[match[1]] = path
    if not pairs:
        raise SystemExit(f"No VIIRS NetCDF files found for {args.year} in the selected Bronze directories.")
    missing = sorted(key for key, pair in pairs.items() if set(pair) != set(PRODUCTS))
    if missing:
        raise SystemExit(f"Re-run bronze for the incomplete swath pairs: {', '.join(missing)}")

    for key, pair in sorted(pairs.items()):
        observed = timestamp(key)
        destination = (SILVER / args.boundary_version
                       / observed.astimezone(KST).strftime("%Y/%m/%d") / f"{key}.parquet")
        write_crop(destination, key, pair, dongs, args.boundary, args.boundary_version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="stage", required=True)
    for stage in ("bronze", "silver"):
        command = commands.add_parser(stage)
        command.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
        command.add_argument("--stop", type=date.fromisoformat, default=date(2026, 1, 1),
                             help="Exclusive end date in Korea Standard Time")
    ingest = commands.choices["bronze"]
    ingest.add_argument("--inventory", action="store_true")
    ingest.add_argument("--bronze-dir", type=Path, default=BRONZE, help="Destination for original NetCDF")
    crop = commands.choices["silver"]
    crop.add_argument("--bronze-dir", action="append", type=Path,
                      help="Read original NetCDF from this directory. Repeat for multiple directories.")
    crop.add_argument("--year", required=True, type=int, choices=(2024, 2025))
    crop.add_argument("--boundary", required=True, type=Path)
    crop.add_argument("--code-column", required=True)
    crop.add_argument("--boundary-version", required=True, help="Boundary snapshot identifier")
    args = parser.parse_args()
    if not date(2024, 1, 1) <= args.start < args.stop <= date(2026, 1, 1):
        parser.error("Select a nonempty date range within 2024–2025 KST.")
    if args.stage == "silver":
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", args.boundary_version):
            parser.error("Use letters, digits, hyphens and underscores for the boundary version.")
        silver(args)
    else:
        bronze(args)


if __name__ == "__main__":
    main()
