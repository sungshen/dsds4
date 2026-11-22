"""폭우 사건별 행정동마다 국토위성 ARD 도엽이 면적의 몇 %를 덮는지 계산한다.

사용:
    python -I scripts/ngii_dong_tile_coverage.py <행정동 경계 geojson(.gz)>

입력
- tables/ngii/rain_region_dongs.csv : 사건 × 행정동 전수 (adm_cd2)
- 도엽 목록
  - 2024: tables/ngii/ard_tiles_2024.csv (사이트 검색 결과, 최신 처리본 기준)
  - 2025: tables/ngii/metadata/ard_metadata_2025.csv (받은 파일)
- 사건 기간: EVENT_PERIOD

방법
- 1:5,000 도엽 번호는 위경도 격자로 바로 바뀐다. 예: 34704006 → 위도 34°대, 경도 127°대, 1:50,000 4번, 1:5,000 6번.
  도엽 하나는 위경도 0.025° × 0.025°다.
- 행정동 안에 약 0.002° 간격으로 점을 찍고, 도엽 안에 드는 점의 비율을 덮인 비율로 본다.

출력: tables/ngii/dong_tile_coverage.csv (사건 × 행정동, 전/후/전체 덮인 비율), 표준출력에 사건별 요약
"""
import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NGII = ROOT / "tables" / "ngii"
EVENT_PERIOD = {"E1": ("2024-07-08", "2024-07-10"), "E2": ("2024-07-17", "2024-07-18"),
                "E3": ("2024-09-20", "2024-09-21"), "E4": ("2025-07-16", "2025-07-20"),
                "E5": ("2025-08-03", "2025-08-03"), "E6": ("2025-08-13", "2025-08-13"),
                "E7": ("2025-09-07", "2025-09-07")}
STEP = 0.002
EPS = 1e-9


def tile_of(lat: float, lon: float) -> str:
    la, lo = math.floor(lat), math.floor(lon)
    r1 = min(int((la + 1 - lat) / 0.25 + EPS), 3)
    c1 = min(int((lon - lo) / 0.25 + EPS), 3)
    top, left = la + 1 - r1 * 0.25, lo + c1 * 0.25
    r2 = min(int((top - lat) / 0.025 + EPS), 9)
    c2 = min(int((lon - left) / 0.025 + EPS), 9)
    return f"{la}{lo % 10}{r1 * 4 + c1 + 1:02d}{r2 * 10 + c2 + 1:03d}"


def phase(ev: str, d: str) -> str:
    s, e = EVENT_PERIOD[ev]
    return "pre" if d < s else "post" if d > e else "during"


def load_tiles():
    t = defaultdict(set)  # (event, phase) -> map numbers
    for r in csv.DictReader((NGII / "ard_tiles_2024.csv").open(encoding="utf-8")):
        t[(r["event_id"], phase(r["event_id"], r["date"]))].add(r["map_number"])
    for r in csv.DictReader((NGII / "metadata" / "ard_metadata_2025.csv").open(encoding="utf-8")):
        d = r["acquisition_date"]
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if "-" not in d else d
        t[(r["event_id"], phase(r["event_id"], d))].add(r["map_number"])
    # E6 07-30 양평 2도엽은 E4 07-30(사건 후)과 같은 파일이라 E6 폴더에만 저장했다.
    for r in csv.DictReader((NGII / "metadata" / "ard_metadata_2025.csv").open(encoding="utf-8")):
        if r["event_id"] == "E6" and r["acquisition_date"].replace("-", "").startswith("20250730"):
            t[("E4", "post")].add(r["map_number"])
    return t


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return polys


def inside(x, y, ring):
    c = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def samples(geom):
    pts = []
    for poly in rings(geom):
        outer, holes = poly[0], poly[1:]
        xs, ys = [p[0] for p in outer], [p[1] for p in outer]
        y = min(ys) + STEP / 2
        while y < max(ys):
            x = min(xs) + STEP / 2
            while x < max(xs):
                if inside(x, y, outer) and not any(inside(x, y, h) for h in holes):
                    pts.append((y, x))
                x += STEP
            y += STEP
    if not pts:  # 아주 작은 동: 꼭짓점 평균
        o = rings(geom)[0][0]
        pts = [(sum(p[1] for p in o) / len(o), sum(p[0] for p in o) / len(o))]
    return pts


def main(boundary: Path) -> None:
    op = gzip.open if boundary.suffix == ".gz" else open
    feats = {f["properties"]["adm_cd2"]: f for f in json.load(op(boundary, "rt", encoding="utf-8"))["features"]}
    meta = list(csv.DictReader((NGII / "metadata" / "ard_metadata_2025.csv").open(encoding="utf-8")))
    bad = [r["map_number"] for r in meta
           if tile_of((float(r["ul_lat"]) + float(r["lr_lat"])) / 2, (float(r["ul_lon"]) + float(r["lr_lon"])) / 2) != r["map_number"]]
    if bad:
        sys.exit(f"도엽 번호 해석이 메타데이터 좌표와 다름: {bad[:5]}")
    tiles = load_tiles()
    cache = {}
    out = []
    for r in csv.DictReader((NGII / "rain_region_dongs.csv").open(encoding="utf-8")):
        ev, cd = r["event_id"], r["adm_cd2"]
        if cd not in cache:
            cache[cd] = [tile_of(y, x) for y, x in samples(feats[cd]["geometry"])]
        ts = cache[cd]
        pre, post = tiles.get((ev, "pre"), set()), tiles.get((ev, "post"), set())
        f = lambda s: round(sum(t in s for t in ts) / len(ts), 3)
        out.append({"event_id": ev, "adm_cd2": cd, "adm_nm": r["adm_nm"],
                    "pre_cov": f(pre), "post_cov": f(post), "any_cov": f(pre | post)})
    with (NGII / "dong_tile_coverage.csv").open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print("사건 | 동 수 | 영상 닿는 동(전/후/전체) | 90% 이상 덮인 동(후)")
    for ev in sorted(EVENT_PERIOD):
        rs = [o for o in out if o["event_id"] == ev]
        n = lambda k, th=0: sum(o[k] > th for o in rs)
        print(ev, len(rs), n("pre_cov"), n("post_cov"), n("any_cov"), sum(o["post_cov"] >= 0.9 for o in rs))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
