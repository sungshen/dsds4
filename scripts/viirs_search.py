"""VIIRS L2G 일별 지표반사도(VNP09GA/VJ109GA/VJ209GA) CMR 전수 검색 -> Bronze search_results.

- 월 단위 [월초, 다음월초) UTC 로 검색하고, 반환된 granule 은 하나도 버리지 않는다.
  (CMR temporal 은 경계를 포함하므로 time_start 가 구간 밖인 항목은 in_period=False 로 표시만 한다.)
- 구름 비율로 제외하지 않는다. paging(CMR-Search-After)을 끝까지 따라간다.
- 원본 검색 응답(JSON 페이지)도 bronze/search_raw/ 에 그대로 저장한다.
- 인증 불필요(CMR 검색은 공개). 키/토큰을 다루지 않는다.
"""
import csv, hashlib, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/viirs.json").read_text())

FIELDS = ["search_id", "source_dataset", "aoi_id", "search_period_start", "search_period_end",
          "product_id", "satellite", "sensor", "observation_time", "observation_time_end",
          "observation_time_precision", "product_type", "tile", "granule_size_mb",
          "day_night_flag", "cloud_cover_cmr", "source_url", "cmr_concept_id", "cmr_updated",
          "in_period", "download_status", "failure_reason", "searched_at"]


def months(start, end):
    y, m = start.year, start.month
    while datetime(y, m, 1, tzinfo=timezone.utc) < end:
        a = datetime(y, m, 1, tzinfo=timezone.utc)
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        yield a, datetime(y2, m2, 1, tzinfo=timezone.utc)
        y, m = y2, m2


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def search(prod, bbox, a, b, raw_dir):
    params = {"collection_concept_id": prod["concept_id"], "temporal": f"{iso(a)},{iso(b)}",
              "bounding_box": ",".join(f"{v:.6f}" for v in bbox),
              "page_size": CFG["page_size"], "sort_key": "start_date"}
    headers, entries, page, hits = {}, [], 0, None
    while True:
        for attempt in range(4):
            try:
                r = requests.get(CFG["cmr_url"], params=params, headers=headers, timeout=120)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt * 3)
        page += 1
        hits = int(r.headers.get("CMR-Hits", -1))
        (raw_dir / f"{prod['short_name']}_{a:%Y-%m}_p{page}.json").write_bytes(r.content)
        batch = r.json()["feed"]["entry"]
        entries += batch
        nxt = r.headers.get("CMR-Search-After")
        if not batch or not nxt or len(entries) >= hits:
            break
        headers = {"CMR-Search-After": nxt}
    return entries, hits, params


def main():
    aoi = gpd.read_file(ROOT / CFG["aoi_file"]).to_crs(4326)
    bbox = list(aoi.total_bounds)  # W,S,E,N
    start = datetime.fromisoformat(CFG["search_period_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(CFG["search_period_end"].replace("Z", "+00:00"))
    raw_dir = ROOT / "data/viirs/bronze/search_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = ROOT / "tables/search_results.csv"
    log = ROOT / "tables/search_log.csv"
    rows, logrows = [], []
    for prod in CFG["products"]:
        for a, b in months(start, end):
            searched_at = iso(datetime.now(timezone.utc))
            entries, hits, params = search(prod, bbox, a, b, raw_dir)
            sid = hashlib.sha1(f"{prod['short_name']}|{CFG['aoi_id']}|{iso(a)}|{iso(b)}".encode()).hexdigest()[:12]
            logrows.append({"search_id": sid, "source_dataset": prod["short_name"], "period_start": iso(a),
                            "period_end": iso(b), "cmr_hits": hits, "rows_saved": len(entries),
                            "bounding_box": params["bounding_box"], "searched_at": searched_at})
            for e in entries:
                url = next((l["href"] for l in e.get("links", []) if l["href"].startswith("https") and l["href"].endswith(".h5")), "")
                ts = e["time_start"]
                t0 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                rows.append({
                    "search_id": sid, "source_dataset": f"{prod['short_name']}.{prod['version']}",
                    "aoi_id": CFG["aoi_id"], "search_period_start": iso(a), "search_period_end": iso(b),
                    "product_id": e["producer_granule_id"] if e.get("producer_granule_id") else e["title"],
                    "satellite": prod["platform"], "sensor": "VIIRS",
                    "observation_time": ts, "observation_time_end": e.get("time_end", ""),
                    "observation_time_precision": "day (L2G daily composite; per-pixel overpass time not in CMR)",
                    "product_type": "L2G daily surface reflectance", "tile": e["title"].split(".")[2],
                    "granule_size_mb": e.get("granule_size", ""), "day_night_flag": e.get("day_night_flag", ""),
                    "cloud_cover_cmr": e.get("cloud_cover", ""), "source_url": url, "cmr_concept_id": e["id"],
                    "cmr_updated": e.get("updated", ""), "in_period": a <= t0 < b,
                    "download_status": "not_attempted", "failure_reason": "", "searched_at": searched_at})
            print(prod["short_name"], f"{a:%Y-%m}", "hits", hits, "saved", len(entries), flush=True)
    for path, data, fields in ((out, rows, FIELDS), (log, logrows, list(logrows[0]))):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(data)
    print("rows", len(rows), "->", out)


if __name__ == "__main__":
    sys.exit(main())
