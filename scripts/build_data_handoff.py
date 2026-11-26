#!/usr/bin/env python3
"""Compile existing sensor audit results into the shared catalog; no processing.

--check verifies committed summary/catalog/checksums without rewriting files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/2026-11-26-data-handoff"


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_bytes(rows):
    s = io.StringIO(newline="")
    w = csv.DictWriter(s, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader(); w.writerows(rows)
    return s.getvalue().encode("utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def compile_summary(folder):
    gk = load(folder / "gk2a/quality_summary.json")
    ng = load(folder / "ngii/validation_summary.json")
    vi = load(folder / "viirs/summary.json")
    footprints = load(folder / "viirs/scene_footprints_summary.json")
    gold = load(folder / "gold/gold_validation_summary.json")
    if gk["status"] != "pass" or gk["errors"] or ng["error_count"]:
        raise ValueError("Local sensor inventories contain integrity errors")
    if vi.get("issue_severity_counts", {}).get("error", 0) or vi.get("small_json_failure_count", 0):
        raise ValueError("VIIRS publication inventory contains errors")
    if (footprints.get("status") != "complete" or footprints.get("exact_single_metadata_matches") != vi["scene_records"]
            or footprints.get("scene_inventory_sha256") != file_sha(folder / "viirs/scenes.csv")):
        raise ValueError("Reported scene footprints must match the current observation inventory")
    if gold["status"] != "PASS" or gold["files_checked"] != vi["object_tier_counts"]["gold"]:
        raise ValueError("Every declared Gold file must pass content validation before handoff")
    if gold["input_inventory_sha256"] != file_sha(folder / "viirs/objects.csv"):
        raise ValueError("Gold validation is not bound to the current object inventory")
    if gold["input_days_sha256"] != file_sha(folder / "viirs/days.csv") or gold["input_scenes_sha256"] != file_sha(folder / "viirs/scenes.csv"):
        raise ValueError("Gold expectations differ from the current daily/scene inventory")
    for gold_key, quality_key in (("pixel_count", "pixel_count"), ("finite_temperature_count", "finite_temperature"),
                                  ("flagged_count", "flagged"), ("valid_count", "valid_flag_zero_temperature")):
        if gold["totals"][gold_key] != vi["quality_totals"][quality_key]:
            raise ValueError("Gold content totals differ from the independent Quality inventory")
    if gold["row_count"] != vi["gold_rows_recorded_server"]:
        raise ValueError("Gold content row total differs from the completed server record")
    inventory = gk["inventory_counts"]
    summary = {
        "handoff_date_kst": "2026-11-26", "period_kst": {"start_inclusive": "2024-01-01", "stop_exclusive": "2026-01-01"},
        "scope": "Existing observations, existing cleaning outputs, new inventories/validation and documentation",
        "metadata_integrity_status": "PASS", "data_reprocessed": False, "new_observations_collected": False,
        "prepared_for_documented_handoff": True, "ready_for_damage_model": False,
        "viirs": {"source_pairs": vi["scene_records"], "original_files": vi["object_tier_counts"]["bronze"],
                  "reported_scene_footprints": footprints["exact_single_metadata_matches"],
                  "published_days": vi["object_tier_counts"]["gold"], "no_paired_cmr_days": len(vi["no_paired_cmr_dates"]),
                  "objects": vi["object_count"], "bytes": vi["object_bytes"],
                  "silver_pixel_records": vi["quality_totals"]["pixel_count"], "gold_rows_verified": gold["row_count"],
                  "gold_bytes_verified": gold["total_gold_bytes"], "valid_temperature_records": gold["totals"]["valid_count"],
                  "temperature_min_k": gold["temperature_min_k"], "temperature_max_k": gold["temperature_max_k"],
                  "cloud_qc_status": "unverified", "inspection": "all metadata/Quality, full Gold bytes/content; no full Bronze/Silver rescan"},
        "gk2a": {"original_files": inventory["originals"], "daily_observations": inventory["selected_daily_observations"],
                 "bronze_only_extras": inventory["bronze_only_extras"], "dongs": inventory["dongs"],
                 "raster_and_mask_files": inventory["silver_and_mask_files"], "qc_rows_reused": inventory["existing_dong_observation_qc_rows_checked"],
                 "units": "packed_DN", "cloud_qc_status": "unverified", "inspection": "full original/raster/mask SHA; all Silver/mask values; existing QC reused"},
        "ngii": {"image_records": ng["image_count"], "image_event_links": ng["event_link_count"],
                 "band_metadata_records": ng["band_record_count"], "provider_mask_records": ng["mask_record_count"],
                 "raw_files_available_here": ng["raw_local_available_count"], "integrity_errors": ng["error_count"],
                 "readiness_gaps": ng["warning_count"], "own_pixel_qc": "not performed", "inspection": "existing ledgers and archived provider XML"},
        "limitations": ["Official damage-scale labels and common team event/region join are not yet available",
                        "Cloud/shadow quality validation is not complete",
                        "SGIS annual boundaries, GK2A boundary hash and NGII vuski 2021-04 require a crosswalk/agreed common geography",
                        "VIIRS 20 NO_PAIRED_CMR dates are conditional search gaps, not zero rainfall/temperature",
                        "GK2A is daily selected packed DN; five extra originals have no selected Silver band",
                        "NGII raw imagery is not present in this environment; archived metadata does not complete pixel QC",
                        "The previously exposed KMA credential must be replaced; current log values are redacted"],
        "access": {"public_repository": "https://github.com/sungshen/dsds4", "gk2a": "existing Git-tracked files",
                   "viirs": "separately delivered 7-day GET links; no bucket-public access change", "ngii_raw": "unavailable in this environment",
                   "signed_links_in_git": False, "hetzner_required": False},
    }
    with (folder / "ngii/image_inventory.csv").open(encoding="utf-8-sig", newline="") as f:
        ng_rows = list(csv.DictReader(f))
    ng_dates = sorted({r["acquisition_date"] for r in ng_rows})
    ng_bytes = sum(int(r["raw_recorded_size_bytes"]) for r in ng_rows)
    catalog = [
        {"dataset_id": "VIIRS_I05_C2", "provider": "NASA LAADS / CMR", "platform_sensor": "Suomi-NPP / VIIRS",
         "source_endpoint": "https://cmr.earthdata.nasa.gov/search/granules.json", "period_start": "2024-01-01", "period_end": "2025-12-31",
         "date_basis": "KST swath-start day", "original_file_count": vi["object_tier_counts"]["bronze"],
         "original_bytes": vi["object_tier_bytes"]["bronze"], "original_location": "R2 configured bucket / viirs-daily/bronze/YYYY/MM/DD/<filename>.nc",
         "cleaned_location": "R2 configured bucket / viirs-daily/{silver,quality,gold}/sgis-YYYY-2q/YYYY/MM/DD/",
         "record_unit": "Silver pixel within swath; Gold dong x swath", "value_units": "I05 brightness temperature Kelvin; TAI93 scan seconds",
         "native_sampling": "VIIRS I05 375m-class geolocated swath; product grid preserved", "geometry": "EPSG:4326 pixel centers; annual SGIS 2Q",
         "availability": "R2 present; 7-day GET download links", "quality": "metadata/Quality and full Gold verified; cloud_qc=unverified",
         "inventory": "viirs/objects.csv;viirs/scenes.csv;gold/gold_validation.csv"},
        {"dataset_id": "GK2A_AMI_IR112_KO", "provider": "Korea Meteorological Administration API Hub", "platform_sensor": "GK-2A / AMI IR112",
         "source_endpoint": "https://apihub.kma.go.kr/api/typ05/api/GK2A/LE1B/IR112/KO/data", "period_start": "2024-01-01", "period_end": "2025-12-31",
         "date_basis": "actual UTC and KST retained; one selected observation per KST day", "original_file_count": inventory["originals"],
         "original_bytes": inventory["original_bytes_checked"], "original_location": "data/bronze/gk2a/IR112_KO/",
         "cleaned_location": "data/silver/gk2a/IR112_KO/<admin_dong_code>/{admin_crop,quality_mask}.tif",
         "record_unit": "dong raster; time bands linked by observations.csv", "value_units": "packed DN uint16; mask uint8",
         "native_sampling": "2km LCC; daily selected archive (not full two-minute cadence)", "geometry": "native LCC preserved; boundary file SHA256; adm_cd2",
         "availability": "public GitHub existing tracked data", "quality": "all original/raster SHA and Silver mask checks passed; cloud_qc=unverified",
         "inventory": "gk2a/original_inventory.csv;gk2a/cleaned_observations.csv;gk2a/silver_raster_inventory.csv"},
        {"dataset_id": "NGII_CAS500_ARD", "provider": "National Geographic Information Institute", "platform_sensor": "CAS500-1 / AEISS-C",
         "source_endpoint": "NGII National Land Information Platform; original catalog IDs in asset_manifest.csv",
         "period_start": ng_dates[0], "period_end": ng_dates[-1], "date_basis": "provider acquisition date; first-input UTC center time retained separately",
         "original_file_count": ng["image_count"], "original_bytes": ng_bytes, "original_location": "recorded data/ngii/<year>/<event>/bronze/originals/<catalog_id>/ (not present here)",
         "cleaned_location": "tables/ngii/metadata/ and reports/2026-11-26-data-handoff/ngii/ (metadata only)",
         "record_unit": "physical ARD tile; separate image-event relation", "value_units": "provider XML values; actual pixel unit verification pending",
         "native_sampling": "provider Level3A 2m product sampling", "geometry": "EPSG:5179 from XML; region tables use vuski 2021-04",
         "availability": "1860 metadata/XML records available; zero raw ZIPs present in checkout", "quality": "metadata integrity passed; own Silver/pixel/cloud QC not performed",
         "inventory": "ngii/image_inventory.csv;ngii/image_event_links.csv;ngii/band_metadata.csv;ngii/provider_mask_metadata.csv"},
    ]
    return summary, catalog


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    summary, catalog = compile_summary(REPORT)
    generated = {REPORT / "summary.json": (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode(),
                 REPORT / "dataset_catalog.csv": csv_bytes(catalog)}
    checksums = []
    for path in sorted(REPORT.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS.csv":
            continue
        data = generated.get(path, path.read_bytes())
        checksums.append({"path": path.relative_to(ROOT).as_posix(), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    # Include the newly generated top-level files on first run too.
    existing = {r["path"] for r in checksums}
    for path, data in generated.items():
        if path.relative_to(ROOT).as_posix() not in existing:
            checksums.append({"path": path.relative_to(ROOT).as_posix(), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    generated[REPORT / "SHA256SUMS.csv"] = csv_bytes(sorted(checksums, key=lambda r: r["path"]))
    for path, data in generated.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                raise ValueError(f"Handoff artifact is stale: {path.relative_to(ROOT)}")
        else:
            path.write_bytes(data)
    print("Handoff metadata and checksums verified" if args.check else "Handoff summary, catalog and checksums written")


if __name__ == "__main__":
    main()
