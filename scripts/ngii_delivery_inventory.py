"""Build a read-only NGII handoff inventory from existing ledgers and archived XML.

No network, raster processing, resampling, or downloads. Python standard library only.
Use --reconcile-event-summary to publish the reconciled summary under tables/ngii;
the old limited-AOI summary is preserved before it is replaced.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "ngii-handoff-v1"
REQUIRED = {
    "event_registry.csv": {"event_id", "event_name", "event_start_kst_date", "event_end_kst_date", "boundary_version"},
    "asset_manifest.csv": {"catalog_id", "event_id", "capture_date", "stored_path", "size_bytes", "sha256"},
    "rain_region_dongs.csv": {"event_id", "sidonm_2021", "adm_cd2"},
    "rain_region_coverage.csv": {"event_id", "adm_cd2", "covered", "site_sigungu_searched"},
    "sweep_sigungu.csv": {"event_id", "sido", "sigungu", "ard_n", "search_period_start", "search_period_end"},
}
META_REQUIRED = {"catalog_id", "event_id", "acquisition_date", "imaging_center_utc", "crs_epsg", "gsd_m", "width", "height", "zip_sha256", "aux_xml", "map_number", "sensor", "satellite", "phase", "product_level"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_rows(path: Path, required: set[str]) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing required columns {sorted(missing)}")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"{path}: malformed CSV row width")
    return rows


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def xml_text(node: ET.Element | None, path: str) -> str:
    return (node.findtext(path, default="") or "").strip() if node is not None else ""


def compact_utc(value: str) -> tuple[str, str]:
    """Normalize an explicitly UTC XML field, preserving all fractional digits."""
    if not value:
        return "", ""
    match = re.fullmatch(r"(\d{14})(\.\d+)?", value)
    if not match:
        raise ValueError(f"invalid compact UTC time: {value}")
    stamp = datetime.strptime(match[1], "%Y%m%d%H%M%S")
    fraction = match[2] or ""
    return (stamp.isoformat() + fraction + "Z",
            (stamp + timedelta(hours=9)).isoformat() + fraction + "+09:00")


def phase(event: dict, date: str) -> str:
    date = datetime.strptime(date.replace("-", ""), "%Y%m%d").date().isoformat()
    if date < event["event_start_kst_date"]:
        return "pre"
    if date > event["event_end_kst_date"]:
        return "post"
    return "during"


def numeric(value: str, minimum: float, maximum: float) -> bool:
    try:
        number = float(value)
        return math.isfinite(number) and minimum <= number <= maximum
    except (ValueError, TypeError):
        return False


def inspect_xml(data: bytes) -> dict:
    root = ET.fromstring(data)
    image = root.find("Image")
    bands = []
    masks = {}
    for node in image if image is not None else []:
        if node.tag.startswith("SR"):
            wave = node.find("BandWavelength/Central")
            bands.append({
                "xml_band": node.tag, "name": xml_text(node, "ImageColor"),
                "file_name": xml_text(node, "ImageFileName"),
                "central_wavelength": wave.text if wave is not None else "",
                "wavelength_unit": wave.get("unit", "unknown") if wave is not None else "unknown",
                "radiance_gain": xml_text(node, "RadianceConversion/Gain"),
                "radiance_offset": xml_text(node, "RadianceConversion/Offset"),
            })
        else:
            masks[node.tag] = {"file_name": xml_text(node, "ImageFileName"),
                               "PerPixelPercentage_raw": xml_text(node, "PerPixelPercentage")}
    sr1 = root.find("Image/SR1")
    corners = {}
    for name in ("UpperLeft", "UpperRight", "LowerLeft", "LowerRight"):
        corners[name] = [float(xml_text(sr1, f"ImagingCoordinates/{name}/Longitude")),
                         float(xml_text(sr1, f"ImagingCoordinates/{name}/Latitude"))]
    inputs = root.findall("Input/InputImages/InputImage")
    first_utc = xml_text(inputs[0], "ImagingTime/ImagingCenterTime/UTC") if inputs else ""
    starts = [xml_text(item, "ImagingTime/ImagingStartTime/UTC") for item in inputs]
    ends = [xml_text(item, "ImagingTime/ImagingEndTime/UTC") for item in inputs]
    times = {"first_input_center": first_utc,
             "input_start_min": min(filter(None, starts), default=""),
             "input_end_max": max(filter(None, ends), default="")}
    return {"root": root, "bands": bands, "masks": masks, "corners": corners,
            "times": times, "input_count": len(inputs)}


def build(repo: Path, out: Path, reconcile: bool = False, verify_raw: bool = False) -> dict:
    ngii = repo / "tables/ngii"
    out.mkdir(parents=True, exist_ok=True)
    issues = []
    sources = []

    def issue(severity: str, check: str, key: str, detail: str) -> None:
        issues.append({"severity": severity, "check": check, "key": key, "detail": detail})

    def source(path: Path, rows: int | str) -> None:
        sources.append({"source_path": str(path.relative_to(repo)), "size_bytes": path.stat().st_size,
                        "sha256": sha256(path), "rows_or_xml_members": rows})

    def read(name: str, columns: set[str]) -> list[dict]:
        path = ngii / name
        rows = csv_rows(path, columns)
        source(path, len(rows))
        return rows

    def index(rows: list[dict], field: str, name: str) -> dict:
        counts = Counter(row[field] for row in rows)
        for key, count in sorted(counts.items()):
            if not key or count > 1:
                issue("ERROR", "duplicate_or_blank_key", name + ":" + key, str(count))
        return {row[field]: row for row in rows}

    tables = {name: read(name, required) for name, required in REQUIRED.items()}
    if (repo / "scripts/ngii_delivery_inventory.py").is_file():
        source(repo / "scripts/ngii_delivery_inventory.py", "generator_code")
    events = index(tables["event_registry.csv"], "event_id", "event_registry")
    if set(events) != {"E1", "E2", "E3", "E4", "E5", "E6", "E7"}:
        issue("ERROR", "event_registry_scope", "event_registry", "Expected canonical NGII event IDs E1 through E7")
    manifest = index(tables["asset_manifest.csv"], "catalog_id", "asset_manifest")
    if not manifest:
        issue("ERROR", "empty_manifest", "asset_manifest", "No image records to deliver")
    metadata = []
    for path in sorted((ngii / "metadata").glob("ard_metadata_*.csv")):
        for line, row in enumerate(csv_rows(path, META_REQUIRED), 2):
            row = {**row, "source_metadata_csv": str(path.relative_to(repo)), "source_metadata_line": line}
            metadata.append(row)
        source(path, sum(1 for row in metadata if row["source_metadata_csv"] == str(path.relative_to(repo))))
    meta = index(metadata, "catalog_id", "metadata")
    xmls = {}
    for path in sorted((ngii / "metadata").glob("ard_aux_xml_*.tar.gz")):
        count = 0
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith("_Aux.xml"):
                    continue
                count += 1
                key = Path(member.name).name.removesuffix("_Aux.xml")
                if key in xmls:
                    issue("ERROR", "duplicate_xml", key, member.name)
                xmls[key] = (archive.extractfile(member).read(), str(path.relative_to(repo)), member.name)
        source(path, count)

    for name, records in tables.items():
        for row in records:
            if row["event_id"] not in events:
                issue("ERROR", "unknown_event", name, row["event_id"])
    for row in tables["sweep_sigungu.csv"]:
        event = events.get(row["event_id"], {})
        if ((row["search_period_start"], row["search_period_end"]) !=
                (event.get("ngii_search_start_date"), event.get("ngii_search_end_date"))):
            issue("ERROR", "search_window_registry_mismatch", row["event_id"], row["sigungu"])
        if not row["ard_n"].isdigit():
            issue("ERROR", "invalid_search_ard_count", row["event_id"], row["sigungu"])
    for key in sorted(set(manifest) ^ set(meta)):
        issue("ERROR", "manifest_metadata_join", key, "missing manifest" if key not in manifest else "missing metadata")
    for key in sorted(set(meta) ^ set(xmls)):
        issue("ERROR", "metadata_xml_join", key, "missing metadata" if key not in meta else "missing archived XML")
    for name in ("rain_region_dongs.csv", "rain_region_coverage.csv"):
        keys = Counter((row["event_id"], row["adm_cd2"]) for row in tables[name])
        for key, count in keys.items():
            if count > 1:
                issue("ERROR", "duplicate_event_dong", name, f"{key}: {count}")
    dong_keys = {(r["event_id"], r["adm_cd2"]) for r in tables["rain_region_dongs.csv"]}
    coverage_keys = {(r["event_id"], r["adm_cd2"]) for r in tables["rain_region_coverage.csv"]}
    for key in sorted(dong_keys ^ coverage_keys):
        issue("ERROR", "region_coverage_join_mismatch", str(key), "event/dong key missing from one table")

    inventory, links, bands_out, masks_out = [], [], [], []
    signatures = defaultdict(list)
    tile_dates = defaultdict(list)
    for key in sorted(set(manifest) | set(meta)):
        m, d = manifest.get(key, {}), meta.get(key, {})
        ev = m.get("event_id") or d.get("event_id", "")
        if ev not in events or key not in xmls or not m or not d:
            continue  # Error above prevents a successful delivery with omitted records.
        event = events[ev]
        try:
            data, archive_path, member = xmls[key]
            x = inspect_xml(data)
            root, corners = x["root"], x["corners"]
            times = {name: compact_utc(value) for name, value in x["times"].items()}
        except (ValueError, ET.ParseError) as exc:
            issue("ERROR", "xml_schema_or_time", key, str(exc))
            continue
        for col, xml_path in (("sensor", "General/Sensor"), ("satellite", "General/Satellite"),
                              ("acquisition_date", "General/AcquisitionDate"), ("crs_epsg", "General/CoordinateReferenceSystem/CSCode"),
                              ("gsd_m", "Image/SR1/ImageGSD/Column"), ("width", "Image/SR1/ImageSize/Width"),
                              ("height", "Image/SR1/ImageSize/Height"), ("map_number", "General/MapNumber")):
            if d.get(col, "") != xml_text(root, xml_path):
                issue("ERROR", "metadata_xml_value_mismatch", key, col)
        if d["imaging_center_utc"] != x["times"]["first_input_center"]:
            issue("ERROR", "metadata_xml_value_mismatch", key, "imaging_center_utc")
        if d["event_id"] != ev or d["acquisition_date"] != m["capture_date"] or d["zip_sha256"] != m["sha256"]:
            issue("ERROR", "manifest_metadata_value_mismatch", key, "event/date/checksum differ")
        if not re.fullmatch(r"[0-9a-f]{64}", m["sha256"]):
            issue("ERROR", "checksum_format", key, m["sha256"])
        if not m["size_bytes"].isdigit() or int(m["size_bytes"]) <= 0:
            issue("ERROR", "invalid_recorded_raw_size", key, m["size_bytes"])
        for col in ("width", "height", "gsd_m"):
            if not numeric(d[col], 0.000001, 100000):
                issue("ERROR", "numeric_outlier", key, col + "=" + d[col])
        if d["crs_epsg"] != "5179" or d["sensor"] != "AEISS-C" or len(x["bands"]) != 4:
            issue("ERROR", "unexpected_product_schema", key, "expected EPSG:5179 AEISS-C four SR bands")
        for corner in corners.values():
            if not numeric(str(corner[0]), 124, 132) or not numeric(str(corner[1]), 32, 39):
                issue("ERROR", "corner_outside_korea_check_bbox", key, str(corner))
        raw_path = Path(m["stored_path"])
        raw_path = raw_path if raw_path.is_absolute() else repo / raw_path
        if not raw_path.resolve().is_relative_to(repo.resolve() / "data/ngii"):
            issue("ERROR", "raw_path_outside_ngii", key, m["stored_path"])
            continue
        exists = raw_path.is_file()
        raw_check = "not_available"
        if exists:
            raw_check = "not_checked"
            if raw_path.stat().st_size != int(m["size_bytes"]):
                issue("ERROR", "raw_size_mismatch", key, m["stored_path"])
            if verify_raw:
                raw_check = "match" if sha256(raw_path) == m["sha256"] else "mismatch"
                if raw_check == "mismatch":
                    issue("ERROR", "raw_checksum_mismatch", key, m["stored_path"])
        p = phase(event, d["acquisition_date"])
        if not times["first_input_center"][1] or phase(event, times["first_input_center"][1][:10]) != p:
            issue("ERROR", "kst_event_phase_mismatch", key, "acquisition date and explicit UTC-to-KST phase differ")
        if d.get("phase") != {"pre": "사건 전", "post": "사건 후", "during": "사건 중"}[p]:
            issue("ERROR", "event_phase_mismatch", key, d.get("phase", ""))
        signatures[m["sha256"]].append(key)
        tile_dates[(d["acquisition_date"], d["map_number"])].append(key)
        row = {
            "catalog_id": key, "stored_event_id": ev, "acquisition_date": datetime.strptime(d["acquisition_date"], "%Y%m%d").date().isoformat(),
            "event_phase": p, "satellite": d["satellite"], "sensor": d["sensor"], "provider_product_level": d.get("product_level", ""),
            "time_reference": "first_input_image_center_not_exact_tile_exposure", "time_source_timezone": "UTC_explicit_XML_element",
            "first_input_center_utc_raw": d["imaging_center_utc"],
            "first_input_center_utc": times["first_input_center"][0], "first_input_center_kst": times["first_input_center"][1],
            "input_start_min_utc": times["input_start_min"][0], "input_end_max_utc": times["input_end_max"][0],
            "input_image_count": x["input_count"], "map_number": d["map_number"], "raster_crs": "EPSG:" + d["crs_epsg"],
            "raster_gsd_column_m": d["gsd_m"], "raster_gsd_row_m": xml_text(root, "Image/SR1/ImageGSD/Row"),
            "raster_width": d["width"], "raster_height": d["height"],
            "corner_coordinate_representation": "provider_XML_longitude_latitude_degrees_datum_not_explicit_here",
            "bbox_min_lon": min(c[0] for c in corners.values()), "bbox_min_lat": min(c[1] for c in corners.values()),
            "bbox_max_lon": max(c[0] for c in corners.values()), "bbox_max_lat": max(c[1] for c in corners.values()),
            "corners_lon_lat_json": json.dumps(corners, separators=(",", ":")),
            "spectral_bands": ";".join(b["name"] for b in x["bands"]),
            "pixel_unit_status": "unverified_no_raster_read_no_scale_applied", "atmospheric_correction_reported": d.get("atmospheric_correction", ""),
            "provider_mask_fields": ";".join(x["masks"]), "provider_mask_value_unit": "unknown_PerPixelPercentage_field_preserved_raw",
            "own_silver_qc_status": "not_performed", "official_damage_label_status": "not_joined",
            "boundary_version_for_region_tables": event["boundary_version"],
            "raw_recorded_path": m["stored_path"], "raw_local_available": str(exists).lower(),
            "raw_recorded_size_bytes": m["size_bytes"], "raw_recorded_sha256": m["sha256"], "raw_checksum_check_now": raw_check,
            "metadata_csv_path": d["source_metadata_csv"], "metadata_csv_line": d["source_metadata_line"],
            "aux_xml_archive": archive_path, "aux_xml_member": member, "aux_xml_sha256": hashlib.sha256(data).hexdigest(),
        }
        inventory.append(row)
        links.append({"catalog_id": key, "event_id": ev, "phase": p, "stored_event_id": ev, "association_basis": "asset_manifest"})
        # Existing NGII coverage rule: these two stored E6 pre-images are E4 post-images too.
        if ev == "E6" and d["acquisition_date"] == "20250730":
            links.append({"catalog_id": key, "event_id": "E4", "phase": phase(events["E4"], d["acquisition_date"]),
                          "stored_event_id": ev, "association_basis": "existing_ngii_dong_tile_coverage_shared_scene_rule"})
        for band in x["bands"]:
            bands_out.append({"catalog_id": key, **band, "output_pixel_unit_status": "unverified",
                              "gain_offset_application": "not_applied_not_validated_for_ARD_pixels"})
        for name, mask in x["masks"].items():
            value = mask["PerPixelPercentage_raw"]
            if value and not numeric(value, 0, 100):
                issue("ERROR", "provider_mask_raw_outlier", key, name + "=" + value)
            masks_out.append({"catalog_id": key, "mask_name": name, **mask, "raw_field_unit": "unknown",
                              "own_pixel_mask_validation": "not_performed"})

    for name, groups in (("duplicate_recorded_sha256", signatures), ("multiple_catalogs_same_date_tile", tile_dates)):
        for key, values in groups.items():
            if len(values) > 1:
                issue("ERROR", name, str(key), ";".join(values))
    unavailable = sum(row["raw_local_available"] == "false" for row in inventory)
    if unavailable:
        issue("WARNING", "raw_missing_locally", "all_images", f"{unavailable}/{len(inventory)} recorded raw paths unavailable in this checkout; no remote availability checked")
    issue("WARNING", "science_qc_not_completed", "all_images", "Provider ARD and XML masks are not own Silver validation; pixel units/mask codes/cloud QC unverified")
    issue("WARNING", "official_damage_labels_not_joined", "all_events", "No official damage labels are joined by this NGII inventory")
    issue("WARNING", "boundary_crosswalk_required", "all_events", "NGII region tables use vuski-2021-04; VIIRS uses SGIS 2024/2025 2Q")

    summary = []
    for ev, event in sorted(events.items()):
        owned = [r for r in inventory if r["stored_event_id"] == ev]
        associated = [r for r in links if r["event_id"] == ev]
        sweep = [r for r in tables["sweep_sigungu.csv"] if r["event_id"] == ev]
        region = [r for r in tables["rain_region_dongs.csv"] if r["event_id"] == ev]
        covered = [r for r in tables["rain_region_coverage.csv"] if r["event_id"] == ev and r["covered"].lower() == "true"]
        target_site_names = {name for row in covered for name in row["site_sigungu_searched"].split("; ") if name}
        target_sweep = [r for r in sweep if r["sido"] + " " + r["sigungu"] in target_site_names]
        linked_dates = sorted({next(r["acquisition_date"] for r in inventory if r["catalog_id"] == link["catalog_id"]) for link in associated})
        summary.append({
            "event_id": ev, "event_name": event["event_name"], "event_start_kst_date": event["event_start_kst_date"],
            "event_end_kst_date": event["event_end_kst_date"], "boundary_version": event["boundary_version"],
            "region_dong_rows": len(region), "region_dongs_with_recorded_site_search": len(covered),
            "site_search_sigungu_rows": len(sweep), "site_search_sigungu_rows_with_ard_hits": sum(r["ard_n"].isdigit() and int(r["ard_n"]) > 0 for r in sweep),
            "target_region_site_sigungu_rows": len(target_sweep),
            "target_region_site_sigungu_rows_with_ard_hits": sum(r["ard_n"].isdigit() and int(r["ard_n"]) > 0 for r in target_sweep),
            "search_result_scope": "recorded_NGII_search_window_and_region_only_not_global_satellite_availability",
            "manifest_owned_image_count": len(owned), "event_linked_image_count": len(associated),
            "linked_from_other_stored_event_count": sum(r["stored_event_id"] != ev for r in associated),
            "linked_pre_image_count": sum(r["phase"] == "pre" for r in associated),
            "linked_during_image_count": sum(r["phase"] == "during" for r in associated),
            "linked_post_image_count": sum(r["phase"] == "post" for r in associated),
            "linked_acquisition_dates": ";".join(linked_dates),
            "owned_raw_local_available_count": sum(r["raw_local_available"] == "true" for r in owned),
            "download_selection_status": event.get("download_selection_status", ""),
            "own_silver_qc_status": "not_performed", "official_damage_label_status": "not_joined",
        })
    # Readiness warnings remain explicit; validation PASS only covers this metadata join.
    errors = sum(issue["severity"] == "ERROR" for issue in issues)
    metrics = {"schema_version": SCHEMA_VERSION, "validation_status": "FAIL" if errors else "PASS_WITH_READINESS_GAPS",
               "scope": "existing_local_ledgers_and_archived_XML_only", "image_count": len(inventory),
               "event_link_count": len(links), "band_record_count": len(bands_out), "mask_record_count": len(masks_out),
               "raw_local_available_count": len(inventory) - unavailable, "raw_missing_local_count": unavailable,
               "per_event_manifest_owned_count": dict(sorted(Counter(r["stored_event_id"] for r in inventory).items())),
               "error_count": errors, "warning_count": len(issues) - errors, "readiness_for_damage_model": "not_ready_missing_raw_access_science_qc_and_damage_labels",
               "checks": ["required_CSV_columns_and_row_width", "catalog_and_event_dong_unique_keys", "manifest_metadata_XML_cardinality",
                          "manifest_metadata_XML_values", "checksum_format_and_cross_ledger_equality", "UTC_parse_and_event_phase",
                          "four_corner_coordinate_ranges", "raster_dimensions_GSD_and_product_schema", "provider_mask_raw_numeric_range",
                          "duplicate_recorded_checksums_and_date_tiles", "local_raw_existence_and_size", "local_raw_SHA256_only_when_requested"],
               "raw_checksum_verification_requested": verify_raw}
    for name, rows in (("image_inventory.csv", inventory), ("image_event_links.csv", links), ("band_metadata.csv", bands_out),
                       ("provider_mask_metadata.csv", masks_out), ("event_summary.csv", summary), ("source_checksums.csv", sources)):
        if rows:
            write_csv(out / name, rows)
    write_csv(out / "validation_findings.csv", issues, ["severity", "check", "key", "detail"])
    (out / "validation_summary.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if reconcile:
        if errors:
            raise ValueError("Refusing to replace event summary because inventory validation failed")
        old = ngii / "event_summary.csv"
        archive = ngii / "event_summary_initial_aoi.csv"
        if old.exists() and not archive.exists():
            shutil.copyfile(old, archive)
        write_csv(old, summary)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=Path("reports/2026-11-26-data-handoff/ngii"))
    parser.add_argument("--reconcile-event-summary", action="store_true")
    parser.add_argument("--verify-raw-checksums", action="store_true", help="Hash local raw ZIPs if available; never downloads")
    args = parser.parse_args()
    out = args.out if args.out.is_absolute() else args.repo_root / args.out
    try:
        result = build(args.repo_root, out, args.reconcile_event_summary, args.verify_raw_checksums)
    except (ValueError, OSError, tarfile.TarError) as exc:
        parser.exit(1, f"NGII inventory failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["error_count"]))


if __name__ == "__main__":
    main()
