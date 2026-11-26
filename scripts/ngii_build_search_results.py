"""국토정보맵 초기 AOI 검색 기록 → search_results.csv + 초기 검색 사건별 요약.

입력: tables/ngii/raw_ard_rows.csv  (브라우저에서 화면 값 그대로 옮긴 행. 결과 0건이면 product_id 빈칸)
출력: tables/ngii/search_results.csv, tables/ngii/event_summary_initial_aoi.csv

이 목록은 초기 제한 AOI 검색 기록이다. 이후 전수 검색·다운로드를 반영한
event_summary.csv는 ngii_delivery_inventory.py --reconcile-event-summary가 만든다.
이 스크립트는 최신 사건 요약을 덮어쓰지 않는다.

규칙
- 화면에 없는 값은 비워 둔다(추측하지 않음).
- 촬영시각은 화면에 시간대 표기가 없으므로 observation_timezone = unknown.
- 같은 product_id가 여러 시군구 검색에서 나오면 한 행으로 합치고 aoi_text에 시군구를 모두 적는다.
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NGII = ROOT / "tables" / "ngii"

EVENTS = {
    "E1": ("2024-07-08", "2024-07-10", "충청·호남·경북 집중호우"),
    "E2": ("2024-07-17", "2024-07-18", "경기북부 집중호우"),
    "E3": ("2024-09-20", "2024-09-21", "남부 집중호우"),
    "E4": ("2025-07-16", "2025-07-20", "충남·광주·경남·경기 폭우"),
    "E5": ("2025-08-03", "2025-08-03", "전남 집중호우"),
    "E6": ("2025-08-13", "2025-08-13", "수도권 집중호우"),
    "E7": ("2025-09-07", "2025-09-07", "전북 군산 폭우"),
}

COLS = [
    "search_id", "event_id", "source_dataset", "aoi_text", "search_period_start", "search_period_end",
    "product_id", "product_type", "satellite", "sensor", "observation_time", "observation_timezone",
    "sheet_or_tile_id", "cloud_cover_percent", "coverage_note", "pixel_size_m", "bands", "file_size",
    "source_url", "download_status", "application_required", "failure_reason", "notes", "searched_at",
]
SOURCE_URL = "https://map.ngii.go.kr/ms/map/nlipCASImgMap.do"


RAW_COLS = ["event_id", "sido", "sigungu", "search_period_start", "search_period_end", "product_id",
            "observation_time", "cloud_cover_percent", "satellite", "sensor", "orbit_direction", "imaging_mode"]


def expand_grouped(path: Path) -> pd.DataFrame:
    """raw_ard_grouped.txt(블록 + '카탈로그ID앞부분,... :: 도엽번호들') → 한 영상 한 행."""
    rows, block = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            head, _, tail = line.partition(" :: ")
            block = head.split(",")
            if tail.strip() == "NONE":
                rows.append(block + [""] * 7)
            continue
        meta, _, sheets = line.strip().partition(" :: ")
        prefix, *rest = meta.split(",")
        for s in sheets.split():
            rows.append(block + [f"{prefix}_{s}"] + rest)
    df = pd.DataFrame(rows, columns=RAW_COLS)
    df.to_csv(NGII / "raw_ard_rows.csv", index=False, encoding="utf-8")
    return df


def main() -> None:
    raw = expand_grouped(NGII / "raw_ard_grouped.txt").fillna("")
    raw["aoi"] = raw["sido"] + " " + raw["sigungu"]
    searched_at = raw.get("searched_at", pd.Series([""] * len(raw)))
    raw["searched_at"] = searched_at

    rows = []
    for ev, g in raw.groupby("event_id", sort=True):
        hits = g[g["product_id"] != ""]
        empty = g[g["product_id"] == ""]
        merged = (
            hits.groupby("product_id", sort=False)
            .agg({
                "aoi": lambda s: "; ".join(dict.fromkeys(s)),
                "search_period_start": "first", "search_period_end": "first",
                "observation_time": "first", "cloud_cover_percent": "first",
                "satellite": "first", "sensor": "first", "orbit_direction": "first",
                "imaging_mode": "first", "searched_at": "first",
            })
            .reset_index()
            .sort_values(["observation_time", "product_id"])
        )
        for _, r in merged.iterrows():
            rows.append({
                "event_id": ev, "source_dataset": "NGII_CAS500-1", "aoi_text": r["aoi"],
                "search_period_start": r["search_period_start"], "search_period_end": r["search_period_end"],
                "product_id": r["product_id"], "product_type": "ARD (국토위성 ARD, L3A)",
                "satellite": r["satellite"], "sensor": r["sensor"],
                "observation_time": r["observation_time"], "observation_timezone": "unknown",
                "sheet_or_tile_id": r["product_id"].rsplit("_", 1)[-1],
                "cloud_cover_percent": r["cloud_cover_percent"],
                "source_url": SOURCE_URL, "download_status": "not_attempted",
                "notes": f"궤도방향={r['orbit_direction']}; 촬영모드={r['imaging_mode']}; "
                         "sheet_or_tile_id는 카탈로그 ID 끝자리(1:5,000 도엽번호로 추정)",
                "searched_at": r["searched_at"],
            })
        for aoi in dict.fromkeys(empty["aoi"]):
            if aoi in set("; ".join(merged["aoi"]).split("; ")):
                continue
            e = empty[empty["aoi"] == aoi].iloc[0]
            rows.append({
                "event_id": ev, "source_dataset": "NGII_CAS500-1", "aoi_text": aoi,
                "search_period_start": e["search_period_start"], "search_period_end": e["search_period_end"],
                "product_type": "ARD (국토위성 ARD, L3A)", "observation_timezone": "unknown",
                "source_url": SOURCE_URL, "download_status": "no_result",
                "notes": "검색 결과 0건", "searched_at": e["searched_at"],
            })

    # 목록만 읽은 검색(카탈로그 ID 미조회): 날짜별 건수 행, 0건이면 no_result 행
    lc_path = NGII / "raw_ard_list_counts.csv"
    if lc_path.exists():
        lc = pd.read_csv(lc_path, dtype=str).fillna("")
        have = {(r["event_id"], a) for r in rows for a in r.get("aoi_text", "").split("; ")}
        for _, r in lc.iterrows():
            aoi = f"{r['sido']} {r['sigungu']}"
            base = {"event_id": r["event_id"], "source_dataset": "NGII_CAS500-1", "aoi_text": aoi,
                    "search_period_start": r["search_period_start"], "search_period_end": r["search_period_end"],
                    "product_type": "ARD (국토위성 ARD, L3A)", "observation_timezone": "unknown",
                    "source_url": SOURCE_URL}
            if r["total_results"] == "0":
                if (r["event_id"], aoi) not in have:
                    rows.append({**base, "download_status": "no_result", "notes": r["notes"] or "검색 결과 0건"})
                continue
            rows.append({**base, "observation_time": r["obs_date"], "cloud_cover_percent": r["cloud_cover_percent"],
                         "download_status": "not_attempted", "satellite": "국토위성1호",
                         "notes": f"목록 기준 {r['n_images']}장(검색 총 {r['total_results']}건), 카탈로그 ID 미조회 → 후보로 고르면 조회"
                                  + (f"; {r['notes']}" if r["notes"] else "")})

    out = pd.DataFrame(rows, columns=COLS).fillna("")
    out["search_id"] = out.groupby("event_id").cumcount().add(1).map("{:02d}".format)
    out["search_id"] = out["event_id"] + "-" + out["search_id"]
    out.to_csv(NGII / "search_results.csv", index=False, encoding="utf-8")

    # 사건별 요약
    summ = []
    hits = out[out["observation_time"] != ""].copy()
    hits["obs_date"] = hits["observation_time"].str[:10]
    for ev, (s, e, name) in EVENTS.items():
        h = hits[hits["event_id"] == ev]
        q = out[out["event_id"] == ev]
        before = h[h["obs_date"] < s]
        after = h[h["obs_date"] > e]
        during = h[(h["obs_date"] >= s) & (h["obs_date"] <= e)]
        by_date = h.groupby("obs_date").agg(
            aoi=("aoi_text", lambda a: ", ".join(sorted({x.split(" ", 1)[-1] for v in a for x in v.split("; ")}))),
            cloud_max=("cloud_cover_percent", lambda c: pd.to_numeric(c, errors="coerce").max()))
        summ.append({
            "event_id": ev, "event": name, "event_start": s, "event_end": e,
            "aoi_searched": "; ".join(dict.fromkeys("; ".join(q["aoi_text"]).split("; "))) if len(q) else "",
            "ard_id_rows": int((h["product_id"] != "").sum()),
            "dates": "; ".join(f"{d}[{r.aoi}](운량최대{r.cloud_max:g})" for d, r in by_date.iterrows()),
            "nearest_before": before["obs_date"].max() if len(before) else "",
            "nearest_after": after["obs_date"].min() if len(after) else "",
            "during_event": "; ".join(sorted(set(during["obs_date"]))),
        })
    pd.DataFrame(summ).to_csv(NGII / "event_summary_initial_aoi.csv", index=False, encoding="utf-8")
    print(out["event_id"].value_counts().sort_index().to_string())
    print(pd.DataFrame(summ)[["event_id", "ard_id_rows", "nearest_before", "nearest_after", "during_event"]].to_string(index=False))


if __name__ == "__main__":
    main()
