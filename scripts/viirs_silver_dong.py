"""Silver 보완: 행정동별 이미지·마스크 생성, silver_index 가이드 컬럼 추가, observation_calendar usable_count 채움.

1) 행정동 마스크(정적, 경계버전별 1회): data/viirs/silver/static/admin_mask/<adm_cd2>_{500m,1km}.tif
   - 해당 동을 감싸는 최소 창(window), float32 1밴드 = pixel 면적 중 동 내부 비율(0~1). 동과 겹치지 않는 pixel = 0.
2) 행정동 이미지(관측별): data/viirs/<product>/<observation_id>/silver/dong/<adm_cd2>_{500m,1km}.tif
   - 서울 crop 에서 위 창만큼 잘라낸 원천 정수값 + mask 밴드 그대로(값 변경 없음). 동 밖 pixel 은 admin mask 로 구분.
3) silver_index 에 가이드 컬럼명 coverage_fraction / valid_fraction / cloud_fraction / shadow_fraction 추가
   (대표값 = 500m 격자 값, representative_grid=500m), 경로 컬럼 image_path_*, admin_area_mask_path_* 추가.
4) observation_calendar.usable_count = 그날 granule 중 '행정동 1개 이상이 valid_fraction_500m >= 0.5' 인 granule 수.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parents[1]
USABLE_RULE = "granule 에서 valid_fraction_500m >= 0.5 인 행정동이 1개 이상"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", help="행정동 이미지를 새로 만들 월(YYYY-MM). 생략 시 전체")
    ap.add_argument("--calendar-only", action="store_true", help="이미지 생성 없이 observation_calendar usable_count 만 갱신")
    args = ap.parse_args()
    months = args.months
    s = pd.read_csv(ROOT / "tables/silver_index.csv.gz", dtype={"adm_cd2": str})
    if args.calendar_only:
        update_calendar(s)
        return
    static = ROOT / "data/viirs/silver/static"
    mdir = static / "admin_mask"
    mdir.mkdir(parents=True, exist_ok=True)
    first = {k: next((ROOT / "data/viirs").glob(f"*/*/silver/admin_crop_{k}.tif")) for k in ("500m", "1km")}
    wins = {}
    for k in ("500m", "1km"):
        w = pd.read_csv(static / f"admin_pixel_weights_{k}.csv", dtype={"adm_cd2": str})
        with rasterio.open(first[k]) as ref:
            prof, T = ref.profile, ref.transform
        for code, g in w.groupby("adm_cd2"):
            r0, r1, c0, c1 = g.row.min(), g.row.max() + 1, g.col.min(), g.col.max() + 1
            win = Window(c0, r0, c1 - c0, r1 - r0)
            arr = np.zeros((r1 - r0, c1 - c0), "float32")
            arr[g.row - r0, g.col - c0] = g.weight
            p = mdir / f"{code}_{k}.tif"
            with rasterio.open(p, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                               crs=prof["crs"], transform=rasterio.windows.transform(win, T), compress="deflate") as ds:
                ds.write(arr, 1)
                ds.set_band_description(1, "admin_inside_fraction")
                ds.update_tags(adm_cd2=code, meaning="pixel 면적 중 행정동 내부 비율(0=동 밖)")
            wins[(code, k)] = (win, str(p.relative_to(ROOT)))
    print("admin masks", len(wins))

    n = 0
    sil_of = {p.parent.name: p for p in (ROOT / "data/viirs").glob("*/*/silver")}
    todo = s if not months else s[s.observation_time.str[:7].isin(months)]
    for oid, grp in todo.groupby("observation_id"):
        sil = sil_of[oid]
        ddir = sil / "dong"
        ddir.mkdir(exist_ok=True)
        for k in ("500m", "1km"):
            with rasterio.open(sil / f"admin_crop_{k}.tif") as src:
                full = src.read()
                prof, T, desc, tags = src.profile, src.transform, src.descriptions, src.tags()
            for code in grp.adm_cd2:
                win, _ = wins[(code, k)]
                sub = full[:, win.row_off:win.row_off + win.height, win.col_off:win.col_off + win.width]
                p = ddir / f"{code}_{k}.tif"
                with rasterio.open(p, "w", driver="GTiff", height=sub.shape[1], width=sub.shape[2], count=sub.shape[0],
                                   dtype=sub.dtype, crs=prof["crs"], transform=rasterio.windows.transform(win, T),
                                   compress="deflate") as ds:
                    ds.write(sub)
                    for i, d in enumerate(desc, 1):
                        ds.set_band_description(i, d)
                    ds.update_tags(**tags, adm_cd2=code)
                n += 1
    print("dong images", n)

    for k in ("500m", "1km"):
        s[f"image_path_{k}"] = [str((sil_of[o] / "dong" / f"{c}_{k}.tif").relative_to(ROOT))
                                for o, c in zip(s.observation_id, s.adm_cd2)]
        s[f"admin_area_mask_path_{k}"] = [wins[(c, k)][1] for c in s.adm_cd2]
        s[f"patch_height_px_{k}"] = [wins[(c, k)][0].height for c in s.adm_cd2]
        s[f"patch_width_px_{k}"] = [wins[(c, k)][0].width for c in s.adm_cd2]
    for c in ("coverage_fraction", "valid_fraction", "cloud_fraction", "shadow_fraction"):
        s[c] = s[f"{c}_500m"]
    s["representative_grid"] = "500m"
    s.to_csv(ROOT / "tables/silver_index.csv.gz", index=False)
    update_calendar(s)


def update_calendar(s):

    cal = pd.read_csv(ROOT / "tables/observation_calendar.csv", dtype=str, keep_default_na=False)
    s["date"] = s.observation_time.str[:10]
    ok = s[s.valid_fraction_500m >= 0.5].groupby(["source_dataset", "date"]).observation_id.nunique()
    down = cal.downloaded_count != ""
    cal.loc[down, "usable_count"] = [str(int(ok.get((d, t), 0))) for d, t in zip(cal.source_dataset[down], cal.date[down])]
    cal["usable_rule"] = np.where(down, USABLE_RULE, "")
    st = cal.status.copy()
    st[down & (cal.usable_count == "0") & (cal.status == "downloaded_qc_pending")] = "quality_excluded"
    st[down & (cal.usable_count != "0") & (cal.status == "downloaded_qc_pending")] = "usable"
    cov = s.groupby(["source_dataset", "date"]).coverage_fraction_500m.max()
    nocov = down & pd.Series([cov.get((d, t), 1.0) == 0 for d, t in zip(cal.source_dataset, cal.date)], index=cal.index)
    st[nocov] = "no_coverage_over_aoi"
    cal["status"] = st
    cal.loc[nocov, "failure_reason"] = "granule 은 있으나 서울(AOI) 픽셀이 모두 fill(관측 없음); 그날 서울 상공 주간 L1B 0건"
    cal.loc[cal.status == "quality_excluded", "failure_reason"] = "Silver QC: valid_fraction_500m >= 0.5 인 행정동 없음(구름·그림자)"
    cal.loc[cal.status == "usable", "failure_reason"] = ""
    cal.to_csv(ROOT / "tables/observation_calendar.csv", index=False)
    print(cal[down].groupby(["source_dataset", "status"]).size())


if __name__ == "__main__":
    main()
