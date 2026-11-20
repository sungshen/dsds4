"""Bronze 완료 체크(가이드 기준) + 다운로드된 granule 의 원천 metadata 요약(granule_metadata.csv).
metadata 요약은 파일 속성을 읽기만 한다(변환·필터 없음). PercentCloudy 등은 타일(약 1,100 km) 전체 값이며 서울 값이 아니다.
"""
import hashlib, re, subprocess
from pathlib import Path

import h5py
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def a(f, k):
    v = f.attrs.get(k)
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode()
    if hasattr(v, "tolist"):
        v = v.tolist()
        return v[0] if isinstance(v, list) and len(v) == 1 else v
    return v


def main():
    sr = pd.read_csv(ROOT / "tables/search_results.csv", dtype=str, keep_default_na=False)
    log = pd.read_csv(ROOT / "tables/search_log.csv")
    man = pd.read_csv(ROOT / "tables/asset_manifest.csv", dtype=str, keep_default_na=False)
    res = []

    def chk(name, ok, detail=""):
        res.append((("PASS" if ok else "FAIL"), name, detail))

    chk("검색 결과 수(CMR-Hits 합) = search_results 행 수", log.cmr_hits.sum() == len(sr), f"{log.cmr_hits.sum()} vs {len(sr)}")
    chk("모든 월 검색에서 CMR-Hits = 저장 행 수(paging 끝까지)", (log.cmr_hits == log.rows_saved).all())
    inp = sr[sr.in_period == "True"]
    chk("기간 내 product_id 중복 없음", not inp.product_id.duplicated().any(),
        f"월 경계 중복(다음 달 1일 granule, in_period=False) {int((sr.in_period == 'False').sum())}행은 검색 원문 그대로 보존")
    files = {str(p.relative_to(ROOT)) for p in (ROOT / "data/viirs").glob("*/*/bronze/originals/*/*") if p.is_file()}
    if "local_retained" not in man.columns:
        man["local_retained"] = ""
    if "release_reason" not in man.columns:
        man["release_reason"] = ""
    released = man[man.local_retained == "False"]
    xml_ok = all((ROOT / q).exists() for q in man[man.asset_kind == "metadata_cmr_xml"].local_path if q)
    chk("로컬에서 내린 원본(.h5)은 sha256·사유 기록, .cmr.xml 은 모두 보존",
        (released.sha256 != "").all() and (released.release_reason != "").all() and xml_ok,
        f"내린 .h5 {len(released)}개 (재다운로드 가능)")
    man = man[man.local_retained != "False"]
    paths = set(man.local_path[man.local_path != ""])
    chk("manifest 경로 ↔ 실제 파일 1:1 (로컬 보관분)", files == paths, f"files {len(files)}, manifest {len(paths)}")
    chk("manifest (product_id, asset) 중복 없음", not man.duplicated(["product_id", "asset_kind"]).any())
    bad = [p for p, s in zip(man.local_path, man.sha256) if p and sha256(ROOT / p) != s]
    chk("모든 파일 sha256 재계산 일치", not bad and (man.sha256[man.local_path != ""] != "").all(), f"불일치 {len(bad)}")
    fail = man[~man.download_status.isin(["downloaded", "already_exists"])]
    chk("실패 asset 은 사유 기록", (fail.failure_reason != "").all(), f"실패 {len(fail)}")
    chk("촬영시각은 원천 metadata 값(UTC 'Z' 포함)", sr.observation_time.str.endswith("Z").all())
    cal = pd.read_csv(ROOT / "tables/observation_calendar.csv", dtype=str, keep_default_na=False)
    z = cal[cal.acquisition_count == "0"]
    chk("0건 날짜는 상태·사유 기록", (z.status != "").all() and (z.failure_reason != "").all(), f"{len(z)}일")
    leak = subprocess.run(["grep", "-rIlE", r"(Bearer [A-Za-z0-9._-]{20,}|eyJ[A-Za-z0-9_-]{30,}|authKey=[A-Za-z0-9]{10,})",
                           "tables", "scripts", "config"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    chk("키/토큰 문자열이 표·코드·설정에 없음", leak == "", leak)

    # 원천 metadata 요약
    meta = []
    for r in man[man.asset_kind == "data_h5"].itertuples():
        with h5py.File(ROOT / r.local_path) as f:
            meta.append({"product_id": r.product_id, "source_dataset": r.source_dataset,
                         "platform": a(f, "PlatformShortName"), "range_beginning": f"{a(f,'RangeBeginningDate')}T{a(f,'RangeBeginningTime')}Z",
                         "range_ending": f"{a(f,'RangeEndingDate')}T{a(f,'RangeEndingTime')}Z",
                         "production_time": a(f, "ProductionTime"), "orbit_numbers": a(f, "OrbitNumberArray"),
                         "day_night_flag": a(f, "DayNightFlag"), "tile_percent_cloudy": a(f, "PercentCloudy"),
                         "tile_percent_shadow": a(f, "PercentShadow"), "tile_percent_land": a(f, "PercentLand"),
                         "tile_qa_percent_good": a(f, "QAPercentGoodQuality"), "tile_qa_percent_missing": a(f, "QAPercentMissingData"),
                         "input_pointer": a(f, "InputPointer")})
    md = pd.DataFrame(meta)
    md["orbit_numbers"] = md.orbit_numbers.map(lambda v: ";".join(str(x) for x in v if x not in (-1, 0)) if isinstance(v, list) else v)
    obs = man[man.asset_kind == "data_h5"].set_index("product_id").observation_time
    chk("파일 속성 시각 범위 = CMR 촬영시각(날짜)",
        all(r.range_beginning[:10] == obs[r.product_id][:10] for r in md.itertuples()))
    gm = ROOT / "tables/granule_metadata.csv"
    if gm.exists():  # 로컬에서 내린 원본의 기존 metadata 행은 유지(누적)
        old = pd.read_csv(gm, dtype=str, keep_default_na=False)
        md = pd.concat([old[~old.product_id.isin(md.product_id)], md.astype(str)], ignore_index=True)
    md.sort_values("product_id").to_csv(gm, index=False)

    out = "\n".join(f"[{s}] {n}" + (f"  ({d})" if d else "") for s, n, d in res)
    (ROOT / "tables/bronze_check.txt").write_text(out + "\n")
    print(out)


if __name__ == "__main__":
    main()
