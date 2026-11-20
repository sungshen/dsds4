"""Silver 정합성 체크 -> tables/silver_check.txt"""
import hashlib, json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"


def main():
    s = pd.read_csv(ROOT / "tables/silver_index.csv.gz", dtype={"adm_cd2": str})
    o = pd.read_csv(ROOT / "tables/silver_observations.csv")
    man = pd.read_csv(ROOT / "tables/asset_manifest.csv", dtype=str, keep_default_na=False)
    dongs = gpd.read_file(ROOT / "config/hangjeongdong_서울특별시.geojson").to_crs(SINU)
    res = []
    chk = lambda n, ok, d="": res.append(f"[{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d else ""))
    n_h5 = (man.asset_kind == "data_h5").sum()
    chk("Bronze granule 수 = Silver 관측 수", len(o) == n_h5, f"{len(o)} vs {n_h5}")
    chk("silver_index 행 = 관측 x 행정동", len(s) == len(o) * len(dongs), f"{len(s)} = {len(o)} x {len(dongs)}")
    chk("(observation_id, adm_cd2) 중복 없음", not s.duplicated(["observation_id", "adm_cd2"]).any())
    fr = [c for c in s.columns if c.endswith(("_fraction_500m", "_fraction_1km"))]
    chk("모든 비율 0~1 (null 제외)", all(s[c].dropna().between(0, 1 + 1e-9).all() for c in fr))
    for k in ("500m", "1km"):
        chk(f"valid <= coverage ({k})", (s[f"valid_fraction_{k}"] <= s[f"coverage_fraction_{k}"] + 1e-9).all())
        chk(f"strict valid <= valid ({k})", (s[f"valid_fraction_strict_{k}"] <= s[f"valid_fraction_{k}"] + 1e-9).all())
    for k, res_m in (("500m", 463.312716527778), ("1km", 926.625433055556)):
        w = pd.read_csv(ROOT / f"data/viirs/silver/static/admin_pixel_weights_{k}.csv", dtype={"adm_cd2": str})
        area = w.groupby("adm_cd2").weight.sum() * res_m ** 2
        true = dongs.set_index("adm_cd2").area
        err = (area / true - 1).abs().max()
        chk(f"pixel 가중치 면적 합 = 행정동 면적 ({k})", err < 1e-6, f"max rel err {err:.2e}")
        chk(f"모든 행정동이 pixel 가중치 보유 ({k})", w.adm_cd2.nunique() == len(dongs))
    idx = ["ndvi_500m", "ndwi_gao_500m", "mndwi_1km", "ndwi_mcfeeters_1km"]
    nul = s[idx[0]].isna() | s[idx[2]].isna()
    chk("지수 null 행은 index_unavailable_reason 기록", s.loc[nul, "index_unavailable_reason"].fillna("").ne("").all())
    chk("지수 값 -1~1", all(s[c].dropna().between(-1, 1).all() for c in idx))
    chk("지수 0 채움 없음(valid 0 인데 값 있는 행 없음)",
        not (s.valid_px_500m.eq(0) & s.ndvi_500m.notna()).any() and not (s.valid_px_1km.eq(0) & s.mndwi_1km.notna()).any())
    bad = 0
    for oid in o.observation_id:
        ci = next((ROOT / "data/viirs").glob(f"*/{oid}/silver/crop_index.json"))
        j = json.loads(ci.read_text())
        for name, h in j["outputs"].items():
            bad += hashlib.sha256((ci.parent / name).read_bytes()).hexdigest() != h
        bad += j["bronze_sha256"] != man.set_index("local_path").loc[j["bronze_file"], "sha256"]
    chk("crop_index checksum(출력·Bronze 계보) 일치", bad == 0, f"불일치 {bad}")
    out = "\n".join(res)
    (ROOT / "tables/silver_check.txt").write_text(out + "\n")
    print(out)


if __name__ == "__main__":
    main()
