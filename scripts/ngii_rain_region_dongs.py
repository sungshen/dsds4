"""폭우 사건별 '폭우 시도'의 모든 행정동 목록 + 국토정보맵 검색 커버리지 더블체크.

입력
- 행정동 경계: vuski/admdongkor 2021-04 (config/boundaries/admdong_kr_vuski_2021_04.geojson.gz, 3,495개 동)
  ※ 경계 버전 통일(SGIS 2024) 전 임시. 경로는 인자로 받는다.
- tables/ngii/sweep_sigungu.csv: 국토정보맵에서 실제로 검색한 (사건, 사이트 시도, 사이트 시군구) 목록과 결과

출력
- tables/ngii/rain_region_dongs.csv : 사건 × 행정동 (ARD 유무와 무관, 전수)
- tables/ngii/rain_region_coverage.csv : 동마다 '검색한 사이트 시군구'와 매칭 결과
- 표준출력: 사건별 시도·시군구·동 수, 매칭 안 된 동

규칙
- 폭우 시도는 사용자 정의 사건 설명(DSDS_HANDOFF / ARD_collection_prompt)의 지역명을 시도 단위로 풀어 정한다(EVENT_SIDO).
- 사이트는 현행 행정구역(전남광주통합특별시, 인천 제물포·영종·서해·검단구, 전북특별자치도 등)을 쓴다.
  2021 경계의 시군구를 사이트 시군구로 옮기는 규칙은 sgg_to_site()에 명시한다(이름 Join은 검색 커버리지 확인 용도만).
"""
import gzip
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NGII = ROOT / "tables" / "ngii"

# 사건 → 폭우 시도 (2021 경계의 시도명)
EVENT_SIDO = {
    "E1": ["충청남도", "충청북도", "대전광역시", "세종특별자치시", "전라북도", "전라남도", "광주광역시", "경상북도"],
    "E2": ["경기도"],
    "E3": ["경상남도", "전라남도", "부산광역시"],
    "E4": ["충청남도", "광주광역시", "경상남도", "경기도"],
    "E5": ["전라남도"],
    "E6": ["서울특별시", "인천광역시", "경기도"],
    "E7": ["전라북도"],
}
EVENT_TEXT = {
    "E1": "2024-07-08~10 충청·호남·경북 집중호우",
    "E2": "2024-07-17~18 경기북부 집중호우",
    "E3": "2024-09-20~21 남부(경남·전남 등) 집중호우",
    "E4": "2025-07-16~20 충남·광주·경남·경기 폭우",
    "E5": "2025-08-03 전남 집중호우",
    "E6": "2025-08-13 수도권 집중호우",
    "E7": "2025-09-07 전북 군산 폭우",
}

SITE_SIDO = {
    "전라북도": "전북특별자치도", "전라남도": "전남광주통합특별시", "광주광역시": "전남광주통합특별시",
    "강원도": "강원특별자치도",
}
# 인천 2026 개편: 2021 중구·동구·서구 → 사이트 신설 구 (동 이름으로 나눔)
INCHEON_YEONGJONG = ("영종", "운서", "용유")
INCHEON_GEOMDAN = ("검단", "불로대곡", "원당", "당하", "오류왕길", "마전", "아라")


def sgg_to_site(sido: str, sgg: str, dong: str) -> list[str]:
    """2021 경계 시군구 → 사이트 시군구 후보(공백 제거 비교용)."""
    s = sgg.replace(" ", "")
    if sido == "인천광역시":
        if sgg == "중구":
            return ["영종구"] if dong.startswith(INCHEON_YEONGJONG) else ["제물포구"]
        if sgg == "동구":
            return ["제물포구"]
        if sgg == "서구":
            return ["검단구"] if dong.startswith(INCHEON_GEOMDAN) else ["서해구"]
    if sido == "경상북도" and sgg == "군위군":
        return ["@대구광역시:군위군"]  # 2023-07 대구 편입
    if sido == "세종특별자치시":
        return ["세종특별자치시"]  # 경계는 '세종시', 사이트는 시도명과 같은 '세종특별자치시'
    return [s]


def main(boundary: Path) -> None:
    feats = json.load(gzip.open(boundary))["features"]
    dongs = pd.DataFrame([f["properties"] for f in feats])
    sweep = pd.read_csv(NGII / "sweep_sigungu.csv", dtype=str).fillna("")
    sweep["sgg_key"] = sweep["sigungu"].str.replace(" ", "", regex=False)

    rows = []
    for ev, sidos in EVENT_SIDO.items():
        sub = dongs[dongs["sidonm"].isin(sidos)]
        searched = sweep[sweep["event_id"] == ev]
        for _, d in sub.iterrows():
            site_sido = SITE_SIDO.get(d["sidonm"], d["sidonm"])
            cands = sgg_to_site(d["sidonm"], d["sggnm"], d["adm_nm"].split()[-1])
            hit = []
            for c in cands:
                sd, key = (c[1:].split(":") if c.startswith("@") else (site_sido, c))
                key = key.replace(" ", "")
                m = searched[(searched["sido"] == sd) &
                             ((searched["sgg_key"] == key) | searched["sgg_key"].str.startswith(key))]
                hit += (m["sido"] + " " + m["sigungu"]).tolist()
            rows.append({
                "event_id": ev, "event": EVENT_TEXT[ev], "sidonm_2021": d["sidonm"], "sggnm_2021": d["sggnm"],
                "adm_nm": d["adm_nm"], "adm_cd2": d["adm_cd2"], "adm_cd": d["adm_cd"],
                "site_sigungu_searched": "; ".join(dict.fromkeys(hit)),
                "covered": bool(hit),
            })
    out = pd.DataFrame(rows)
    out.drop(columns=["site_sigungu_searched", "covered"]).to_csv(NGII / "rain_region_dongs.csv", index=False, encoding="utf-8")
    out.to_csv(NGII / "rain_region_coverage.csv", index=False, encoding="utf-8")

    print("사건별 폭우 시도·시군구·행정동 수 (경계: vuski 2021-04)")
    for ev, g in out.groupby("event_id"):
        print(f"{ev} {EVENT_TEXT[ev]}: 시도 {g.sidonm_2021.nunique()}, 시군구 {g[['sidonm_2021','sggnm_2021']].drop_duplicates().shape[0]}, "
              f"행정동 {len(g)}, 검색 커버 {int(g.covered.sum())}, 미커버 {int((~g.covered).sum())}")
    miss = out[~out["covered"]]
    if len(miss):
        print("\n미커버 시군구:")
        print(miss.groupby(["event_id", "sidonm_2021", "sggnm_2021"]).size().to_string())


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config/boundaries/admdong_kr_vuski_2021_04.geojson.gz")
