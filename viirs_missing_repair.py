#!/usr/bin/env python3
"""Read-only CMR diagnosis of VIIRS dates absent from an R2 audit.

Checks every Collection 2 VNP02IMG/VNP03IMG pair intersecting the configured Korean
BBOX during each KST day. It never downloads source imagery or writes R2 objects.
Recovery belongs to viirs-full-rebuild-hetzner/viirs_full_rebuild.py; the deprecated
nearest-noon repair mode is deliberately disabled.

Example:
  python viirs_missing_repair.py --audit r2_audit_report/daily_status.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from viirs_r2_audit import make_client


def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=".env", help="Local R2 dotenv file")
    p.add_argument("--audit", default="r2_audit_report/daily_status.csv", help="CSV produced by viirs_r2_audit.py")
    p.add_argument("--prefix", default=None, help="Override R2_PREFIX (default: viirs-daily)")
    p.add_argument("--out", default="cmr_missing_report", help="Local results directory")
    p.add_argument("--apply", action="store_true", help="Deprecated; rejected to prevent obsolete one-scene ingestion")
    return p.parse_args()


def load_candidates(path: Path) -> list[date]:
    if not path.is_file():
        raise FileNotFoundError(f"Audit CSV not found: {path}")
    selected: set[date] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = csv.DictReader(f)
        if not {"date", "status"}.issubset(set(rows.fieldnames or [])):
            raise ValueError("Audit CSV must have date and status columns")
        for row in rows:
            if row["status"] != "UNVERIFIED_NO_OBJECTS":
                continue
            dt = date.fromisoformat(row["date"])
            if dt.year not in (2024, 2025):
                raise ValueError(f"Refusing unexpected year: {dt}")
            selected.add(dt)
    return sorted(selected)


def one_day_cmr(pipeline, session: requests.Session, day: date) -> dict:
    """All paired scene starts in the KST day; no nearest-noon selection."""
    start = datetime.combine(day, time.min, pipeline.KST).astimezone(timezone.utc)
    stop = datetime.combine(day + timedelta(days=1), time.min, pipeline.KST).astimezone(timezone.utc)
    first, second = pipeline.PRODUCTS
    imagery = pipeline.search(session, first, start, stop)
    geo = pipeline.search(session, second, start, stop)
    # The source search already filters start <= timestamp < stop. Keep the
    # constraint explicit so diagnostics remain correct if that helper changes.
    imagery = {key: value for key, value in imagery.items() if start <= pipeline.timestamp(key) < stop}
    geo = {key: value for key, value in geo.items() if start <= pipeline.timestamp(key) < stop}
    paired = sorted(set(imagery) & set(geo))
    unpaired = sorted(set(imagery) ^ set(geo))
    return {"status": "CMR_UNPAIRED_PRODUCTS" if unpaired else
                      "CMR_PAIR_FOUND" if paired else "NO_PAIRED_CMR_OBSERVATION",
            "cmr_imagery": len(imagery), "cmr_geolocation": len(geo),
            "cmr_pairs": len(paired), "unpaired_count": len(unpaired),
            "swaths": paired, "unpaired_swaths": unpaired}


def day_prefixes(prefix: str, day: date) -> dict[str, str]:
    path = day.strftime("%Y/%m/%d")
    version = f"sgis-{day.year}-2q"
    return {"bronze": f"{prefix}/bronze/{path}/",
            **{tier: f"{prefix}/{tier}/{version}/{path}/"
               for tier in ("silver", "quality", "gold")}}


def r2_day_keys(client, bucket: str, prefix: str, day: date) -> dict[str, list[str]]:
    found = {}
    for tier, location in day_prefixes(prefix, day).items():
        paginator = client.get_paginator("list_objects_v2")
        found[tier] = [entry["Key"] for page in paginator.paginate(Bucket=bucket, Prefix=location)
                       for entry in page.get("Contents", []) if not entry["Key"].endswith("/")]
    return found


def save_reports(path: Path, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    fields = ["date", "status", "cmr_imagery", "cmr_geolocation", "cmr_pairs", "unpaired_count", "swaths",
              "unpaired_swaths", "r2_bronze", "r2_silver", "r2_quality", "r2_gold", "detail"]
    with (path / "cmr_daily_status.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**row, "swaths": json.dumps(row.get("swaths", [])),
                         "unpaired_swaths": json.dumps(row.get("unpaired_swaths", []))} for row in rows)
    tally = Counter(row["status"] for row in rows)
    summary = {"mode": "READ_ONLY", "checked_at_utc": datetime.now(timezone.utc).isoformat(),
               "days_checked": len(rows), "status_counts": dict(tally),
               "products": ["VNP02IMG", "VNP03IMG"], "collection": "2",
               "bbox": [124, 32, 132, 39], "time_basis": "KST day, granule start time",
               "results": str(path / "cmr_daily_status.csv"), "cautions": [
                   "No matching CMR product pairs under this BBOX/time query does not prove there was no satellite pass.",
                   "BBOX intersection does not guarantee pixels inside the administrative polygons.",
                   "This script never modifies R2; use the all-observations rebuild for recovery."]}
    (path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = arguments()
    if args.apply:
        raise ValueError("--apply is disabled: the old repair selected only one scene per day. "
                         "Use viirs-full-rebuild-hetzner/viirs_full_rebuild.py for all-observations recovery.")
    load_dotenv(args.env, override=False)
    days = load_candidates(Path(args.audit))
    if not days:
        print("No UNVERIFIED_NO_OBJECTS dates in audit CSV; nothing to do.")
        return 0
    import viirs_pipeline as pipeline

    args.bucket = args.account_id = args.endpoint_url = None
    args.workers = 1
    client, bucket = make_client(args)
    prefix = (args.prefix or os.getenv("R2_PREFIX") or "viirs-daily").strip("/")
    if not prefix:
        raise ValueError("R2 prefix cannot be empty")
    client.head_bucket(Bucket=bucket)
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    rows = []
    out = Path(args.out)
    print(f"Mode: READ_ONLY | candidates: {len(days)} dates")
    try:
        for index, day in enumerate(days, start=1):
            record = {"date": day.isoformat(), "status": "", "detail": ""}
            try:
                existing = r2_day_keys(client, bucket, prefix, day)
                record.update({f"r2_{tier}": len(keys) for tier, keys in existing.items()})
                if any(existing.values()):
                    record["status"] = "R2_ALREADY_HAS_OBJECTS_SKIPPED"
                    record["detail"] = "Rerun the audit; this date changed since the previous report"
                else:
                    record.update(one_day_cmr(pipeline, session, day))
            except Exception as exc:
                record["status"] = "ERROR"
                # API exception messages may include endpoints, never persist credentials.
                record["detail"] = type(exc).__name__
            rows.append(record)
            print(f"[{index}/{len(days)}] {day}: {record['status']}", flush=True)
            save_reports(out, rows)
    finally:
        session.close()
    print(f"Saved: {out.resolve()}/cmr_daily_status.csv")
    print("Results:", dict(Counter(row["status"] for row in rows)))
    return 2 if any(row["status"] in ("ERROR", "CMR_UNPAIRED_PRODUCTS") for row in rows) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
