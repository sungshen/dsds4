#!/usr/bin/env python3
"""Describe existing VIIRS scenes using exact-name public NASA CMR metadata.

No imagery downloads, temporal/spatial scene discovery, missing-day searches,
R2 requests, or changes to existing inventory files. Public metadata batches
are cached privately; reruns reuse successful and explicit missing results.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
DOCS = "https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html"
EXTENT_BASIS = "NASA CMR granule footprint; not cropped land coverage"
NAME = re.compile(r"VNP02IMG\.(A\d{7}\.\d{4})\.002\.\d{13}\.nc")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path, payload, private=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if private:
        temporary.chmod(0o600)
    os.replace(temporary, path)


def producer_id(item):
    identifiers = item.get("umm", {}).get("DataGranule", {}).get("Identifiers", [])
    names = [value.get("Identifier") for value in identifiers if value.get("IdentifierType") == "ProducerGranuleId"]
    if len(names) != 1:
        raise ValueError("CMR record lacks one exact ProducerGranuleId")
    return names[0]


def points_in(value):
    """Retain original geometry; use explicit UMM lon/lat nodes for envelopes."""
    points = []
    if isinstance(value, dict):
        if "Longitude" in value and "Latitude" in value:
            lon, lat = float(value["Longitude"]), float(value["Latitude"])
            if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("CMR coordinates outside degree bounds")
            points.append((lon, lat))
        for child in value.values():
            points.extend(points_in(child))
    elif isinstance(value, list):
        for child in value:
            points.extend(points_in(child))
    return points


def longitude_envelope(longitudes):
    """Shortest circular coordinate envelope; west>east denotes date-line wrap."""
    values = sorted(set(value % 360 for value in longitudes))
    if not values:
        raise ValueError("No longitude coordinates")
    if len(values) == 1:
        west = east = (values[0] + 180) % 360 - 180
        return west, east, 0.0
    gaps = [((values[(index + 1) % len(values)] - value) % 360, index) for index, value in enumerate(values)]
    largest_gap, index = max(gaps)
    west = (values[(index + 1) % len(values)] + 180) % 360 - 180
    east = (values[index] + 180) % 360 - 180
    return west, east, 360 - largest_gap


def geometry_extent(spatial):
    geometry = spatial.get("HorizontalSpatialDomain", {}).get("Geometry", {})
    rectangles = geometry.get("BoundingRectangles", [])
    points = points_in(geometry)
    for box in rectangles:
        west, south, east, north = (float(box[key]) for key in (
            "WestBoundingCoordinate", "SouthBoundingCoordinate", "EastBoundingCoordinate", "NorthBoundingCoordinate"))
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("Invalid reported CMR bounding rectangle")
        points.extend([(west, south), (east, north)])
    if not points:
        return {"status": "NO_REPORTED_GEOMETRY", "bbox_basis": "unavailable"}
    if len(rectangles) == 1:
        box = rectangles[0]
        west, south, east, north = (float(box[key]) for key in (
            "WestBoundingCoordinate", "SouthBoundingCoordinate", "EastBoundingCoordinate", "NorthBoundingCoordinate"))
        width = (east - west) % 360
        if west == -180 and east == 180:
            width = 360.0
        basis = "CMR reported bounding rectangle"
    else:
        west, east, width = longitude_envelope([point[0] for point in points])
        south, north = min(point[1] for point in points), max(point[1] for point in points)
        basis = "circular envelope of reported coordinates; not a geodesic-edge or pixel-footprint calculation"
    return {"status": "REPORTED_FOOTPRINT", "bbox_west": west, "bbox_south": south,
            "bbox_east": east, "bbox_north": north, "bbox_wsen_json": canonical([west, south, east, north]),
            "longitude_span_degrees": width, "crosses_antimeridian": west > east,
            "reported_coordinate_count": len(points), "reported_polygon_count": len(geometry.get("GPolygons", [])),
            "reported_rectangle_count": len(rectangles), "bbox_basis": basis,
            "cmr_coordinate_system_declared": geometry.get("CoordinateSystem", "")}


def batch_metadata(names, cache, offline=False, refresh=False):
    names = sorted(names)
    query_hash = digest({"endpoint": ENDPOINT, "provider": "LAADS", "short_name": "VNP02IMG", "version": "2", "exact_names": names})
    cache_path = cache / (query_hash + ".json")
    retrieval = "public_cmr_metadata_post"
    if cache_path.is_file() and not refresh:
        saved = json.loads(cache_path.read_text())
        if saved.get("query_sha256") != query_hash or saved.get("requested_names") != names:
            raise ValueError("CMR cache request identity mismatch")
        raw = base64.b64decode(saved["body_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != saved.get("response_sha256"):
            raise ValueError("CMR cache response checksum mismatch")
        retrieval = "cached_public_cmr_metadata"
    else:
        if offline:
            raise ValueError("CMR metadata cache is missing")
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(["POST"]))))
        parameters = [("provider", "LAADS"), ("short_name", "VNP02IMG"), ("version", "2"),
                      ("page_size", str(max(100, len(names) * 2))),
                      ("options[producer_granule_id][pattern]", "false"),
                      ("options[producer_granule_id][ignore_case]", "false")]
        parameters.extend(("producer_granule_id[]", name) for name in names)
        with session.post(ENDPOINT, data=parameters, timeout=(15, 90),
                          headers={"Client-Id": "viirs-existing-scene-footprint-handoff"}) as response:
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("Refusing oversized CMR metadata response")
            raw = response.content
        session.close()
        saved = {"query_sha256": query_hash, "requested_names": names,
                 "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                 "response_sha256": hashlib.sha256(raw).hexdigest(),
                 "body_base64": base64.b64encode(raw).decode()}
        write_json(cache_path, saved, private=True)
    payload = json.loads(raw)
    items = payload.get("items", [])
    if payload.get("hits", 0) != len(items):
        raise ValueError("CMR exact-name response was truncated; no broad follow-up query is allowed")
    found = defaultdict(list)
    for item in items:
        name = producer_id(item)
        if name not in names:
            raise ValueError("CMR returned an unrequested granule")
        if item.get("meta", {}).get("provider-id") != "LAADS":
            raise ValueError("CMR provider mismatch")
        collection = item.get("umm", {}).get("CollectionReference", {})
        if collection.get("ShortName") != "VNP02IMG" or str(collection.get("Version")) not in ("2", "002"):
            raise ValueError("CMR product/collection mismatch")
        found[name].append(item)
    return names, found, {key: saved[key] for key in ("query_sha256", "retrieved_at_utc", "response_sha256")}, retrieval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenes", type=Path, default=ROOT / "reports/2026-11-26-data-handoff/viirs/scenes.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/2026-11-26-data-handoff/viirs")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/platform/viirs-footprints-cache")
    parser.add_argument("--batch-size", type=int, choices=(50, 100), default=100)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    with args.scenes.open(newline="", encoding="utf-8") as stream:
        scenes = list(csv.DictReader(stream))
    by_name, identities = {}, set()
    for scene in scenes:
        filename = scene["VNP02IMG_filename"]
        match = NAME.fullmatch(filename)
        identity = (scene["date_kst"], scene["swath_id"])
        if not match or match[1] != scene["swath_id"] or filename in by_name or identity in identities:
            raise ValueError("Existing scene inventory contains duplicate or mismatched identities")
        by_name[filename] = scene
        identities.add(identity)
    names = sorted(by_name)
    batches = [names[index:index + args.batch_size] for index in range(0, len(names), args.batch_size)]
    rows, batch_receipts, failed_batches = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = {pool.submit(batch_metadata, batch, args.cache, args.offline, args.refresh): batch for batch in batches}
        for future in as_completed(tasks):
            batch = tasks[future]
            try:
                requested, found, receipt, retrieval = future.result()
                batch_receipts.append({**receipt, "requested_count": len(requested), "matched_record_count": sum(map(len, found.values())), "retrieval": retrieval})
            except Exception as exc:
                found, receipt, retrieval = {}, {}, "batch_failed"
                failed_batches.append({"query_sha256": digest(batch), "requested_count": len(batch), "reason": type(exc).__name__})
            for name in batch:
                scene = by_name[name]
                records = found.get(name, [])
                row = {"date_kst": scene["date_kst"], "swath_id": scene["swath_id"],
                       "source_granule_id": name, "product_id": "VNP02IMG", "collection_version": "002",
                       "extent_basis": EXTENT_BASIS, "crs": "WGS 84 geographic longitude/latitude",
                       "coordinate_order": "west,south,east,north; raw UMM nodes explicitly name Longitude/Latitude",
                       "cropped_land_coverage_fraction": "", "cloud_qc_status": "unverified",
                       "retrieval": retrieval, **receipt, "cmr_exact_name_matches": len(records)}
                if len(records) != 1:
                    row["status"] = "BATCH_REQUEST_FAILED" if retrieval == "batch_failed" else "MISSING_EXACT_CMR_METADATA" if not records else "AMBIGUOUS_EXACT_CMR_METADATA"
                    rows.append(row)
                    continue
                item = records[0]
                spatial = item.get("umm", {}).get("SpatialExtent", {})
                meta = item.get("meta", {})
                row.update(cmr_concept_id=meta.get("concept-id", ""), cmr_revision_id=meta.get("revision-id", ""),
                           cmr_revision_date=meta.get("revision-date", ""),
                           reported_spatial_extent_json=canonical(spatial),
                           footprint_record_sha256=digest({"source_granule_id": name, "spatial_extent": spatial, "concept_id": meta.get("concept-id"), "revision_id": meta.get("revision-id")}))
                try:
                    row.update(geometry_extent(spatial))
                except (ValueError, TypeError, KeyError):
                    row.update(status="INVALID_REPORTED_GEOMETRY", bbox_basis="unavailable; raw reported geometry retained")
                rows.append(row)
    rows.sort(key=lambda row: (row["date_kst"], row["swath_id"]))
    if len(rows) != len(scenes) or len({(row["date_kst"], row["swath_id"]) for row in rows}) != len(scenes):
        raise ValueError("Output scene count/identity mismatch")
    args.out.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    path = args.out / "scene_footprints.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    summary = {"status": "complete" if all(row["status"] == "REPORTED_FOOTPRINT" for row in rows) else "explicit_unavailable_records_present",
               "source": "Public NASA CMR exact existing VNP02IMG granule metadata",
               "endpoint": ENDPOINT, "documentation": DOCS,
               "scene_inventory_sha256": hashlib.sha256(args.scenes.read_bytes()).hexdigest(),
               "scene_count": len(scenes), "unique_scene_count": len(identities),
               "status_counts": dict(Counter(row["status"] for row in rows)),
               "exact_single_metadata_matches": sum(row["cmr_exact_name_matches"] == 1 for row in rows),
               "antimeridian_wrapping_envelopes": sum(row.get("crosses_antimeridian") is True for row in rows),
               "bbox_basis_counts": dict(Counter(row.get("bbox_basis", "unavailable") for row in rows)),
               "batch_size": args.batch_size, "metadata_batches": len(batches),
               "batch_retrieval_counts": dict(Counter(row["retrieval"] for row in batch_receipts)),
               "response_bytes_sha256_receipts": sorted(batch_receipts, key=lambda row: row["query_sha256"]),
               "failed_batches": failed_batches,
               "output_csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "extent_basis": EXTENT_BASIS, "cloud_qc_status": "unverified",
               "new_images_or_scenes_collected": 0, "r2_requests": 0,
               "limitations": ["These are the provider's nominal granule footprints, not clipped land coverage or per-pixel geolocation checks.",
                               "Raw UMM geometry is retained. Derived polygon envelopes use reported vertices; geodesic arcs can extend beyond vertex latitude extrema.",
                               "west>east means antimeridian wrap; raw geometry is not rewritten or clipped.",
                               "Missing/ambiguous metadata remains explicit and is never replaced with the CMR query BBOX.",
                               "Cached metadata describes its retrieval time and revision; reruns reuse it unless --refresh is requested."],
               }
    write_json(args.out / "scene_footprints_summary.json", summary)
    print(json.dumps({key: summary[key] for key in ("status", "scene_count", "status_counts", "metadata_batches", "batch_retrieval_counts")}))
    return 1 if failed_batches else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
