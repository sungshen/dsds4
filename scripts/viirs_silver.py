"""VIIRS L2G(VNP09GA/VJ109GA/VJ209GA) Silver: 관측(granule) x 서울 행정동 crop·mask·품질검증·지수.

입력 : tables/asset_manifest.csv 의 Bronze .h5 (읽기 전용), config/hangjeongdong_서울특별시.geojson
출력 : data/viirs/<product>/<observation_id>/silver/
         admin_crop_500m.tif  I1,I2,I3 원천 정수값(scale 1e-4, nodata -28672) + mask_500m
         admin_crop_1km.tif   M1..M11 원천 정수값 + QF1..QF7 + land_water_mask + 각도 + mask_1km
         crop_index.json      계보·처리 정보
       data/viirs/silver/static/admin_pixel_weights_{500m,1km}.csv  (행정동 x pixel 겹침 면적비)
       tables/silver_index.csv.gz       관측 x 행정동 1행
       tables/silver_observations.csv   관측 1행(서울 전체 요약)
       tables/overpass_candidates.csv   관측일 서울 상공 주간 L1B granule 시각(CMR)
       docs/viirs_silver_codebook.md 는 별도 작성.

원칙: 원천 격자 유지(재투영·resize 없음), 0 채움 금지(null + 사유), padding 없음.
비율 분모: total = 행정동 내부 pixel 면적(겹침 면적비 합, pixel 단위). covered = 관측 있음.
          valid = covered 중 mask 통과. cloud/shadow fraction = covered 중 비율.
QA 비트 정의 출처: 원천 HDF5 각 QF 변수의 'QA index' 속성 (docs/viirs_silver_codebook.md 에 전문).
"""
import argparse, json, hashlib, sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import h5py
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.transform import from_origin
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/viirs.json").read_text())
PROCESSING_VERSION = "viirs_silver_v2"
SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
FILL_SR = -28672
SCALE = 1e-4
BOUNDARY_VERSION = "unknown (config/hangjeongdong_서울특별시.geojson, commit 588a19d7, 2026-11-02; 출처·기준일 미확인)"
GRIDS = {
    "500m": {"group": "VIIRS_Grid_500m_2D", "n": 2400, "native": "375 m (I-band, nadir)"},
    "1km": {"group": "VIIRS_Grid_1km_2D", "n": 1200, "native": "750 m (M-band, nadir)"},
}
L1B = {"VNP09GA": "VNP03MOD", "VJ109GA": "VJ103MOD", "VJ209GA": "VJ203MOD"}
# mask bit (uint16)
M_FILL, M_NIGHT, M_CLOUD, M_SHADOW, M_SNOW, M_BADSDR, M_QAMIS, M_ADJ, M_CIRRUS, M_PROBCLEAR, M_AOTQ = 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024
# v2: 밴드별 'overall quality SR bad'(QF5/QF6) 는 QF4 b4 'overall quality of AOT bad' 와 100% 동일하게 켜짐을 확인
#     (2024-07 VJ109GA 맑은 육지 pixel 기준) -> 밴드 불량이 아니라 에어로졸 보정 신뢰도이므로 기본 제외에서 빼고 strict 에만 적용.
EXCLUDE = M_FILL | M_NIGHT | M_CLOUD | M_SHADOW | M_SNOW | M_BADSDR | M_QAMIS
EXCLUDE_STRICT = EXCLUDE | M_AOTQ | M_ADJ
MASK_POLICY = ("standard exclude: fill/no-obs, night(QF1 b4), cloud probably/confident(QF1 b2-3>=2), shadow(QF2 b3), "
               "snow/ice(QF2 b5), bad SDR of index bands(QF3/QF4), 500m: iobs_res_1!=0 (1km QA 와 다른 관측) | "
               "strict = standard + AOT/SR overall quality bad(QF4 b4 = QF5/QF6) + adjacent cloud(QF7 b1) | "
               "record-only: thin cirrus(QF2 b6-7, QF7 b4), probably clear(QF1 b2-3=01)")


def bit(a, b, n=1):
    return (a >> b) & ((1 << n) - 1)


def grid_meta(f, g):
    sm = f["HDFEOS INFORMATION/StructMetadata.0"][()].decode()
    blk = sm.split(f'GridName="{g}"')[1]
    ul = [float(v) for v in blk.split("UpperLeftPointMtrs=(")[1].split(")")[0].split(",")]
    lr = [float(v) for v in blk.split("LowerRightMtrs=(")[1].split(")")[0].split(",")]
    n = int(blk.split("XDim=")[1].split("\n")[0])
    return ul, (lr[0] - ul[0]) / n, n


def window_for(bounds, ul, res, pad=2):
    x0, y0, x1, y1 = bounds
    c0 = int(np.floor((x0 - ul[0]) / res)) - pad
    c1 = int(np.ceil((x1 - ul[0]) / res)) + pad
    r0 = int(np.floor((ul[1] - y1) / res)) - pad
    r1 = int(np.ceil((ul[1] - y0) / res)) + pad
    return r0, r1, c0, c1


def pixel_weights(dongs, ul, res, win):
    """행정동 x pixel 겹침 면적 / pixel 면적 (sinusoidal = 등적)."""
    r0, r1, c0, c1 = win
    cells = []
    for r in range(r0, r1):
        for c in range(c0, c1):
            x = ul[0] + c * res
            y = ul[1] - r * res
            cells.append((r - r0, c - c0, box(x, y - res, x + res, y)))
    px = gpd.GeoDataFrame({"row": [a for a, _, _ in cells], "col": [b for _, b, _ in cells]},
                          geometry=[g for _, _, g in cells], crs=SINU)
    ov = gpd.overlay(dongs[["adm_cd2", "geometry"]], px, how="intersection", keep_geom_type=True)
    ov["weight"] = ov.area / (res * res)
    return ov[["adm_cd2", "row", "col", "weight"]].query("weight > 1e-9").reset_index(drop=True)


def read(g, name, win):
    r0, r1, c0, c1 = win
    return g[name][r0:r1, c0:c1]


def overpass_candidates(prod, date):
    r = requests.get(CFG["cmr_url"], params={"short_name": L1B[prod], "temporal": f"{date}T00:00:00Z,{date}T23:59:59Z",
                                              "bounding_box": "126.764278,37.429011,127.183552,37.701080",
                                              "day_night_flag": "day", "page_size": 50}, timeout=60)
    r.raise_for_status()
    return sorted((e["time_start"], e["time_end"]) for e in r.json()["feed"]["entry"])


def wmean(v, w):
    s = w.sum()
    return float((v * w).sum() / s) if s > 0 else None


def index_px(a, b):
    """(a-b)/(a+b). 분모 0, 결측, 음수 반사도(원천 valid_range 는 -0.01 까지 허용; 수면 대기보정 잔차) -> nan."""
    a = a.astype("float64"); b = b.astype("float64")
    den = a + b
    out = np.full(a.shape, np.nan)
    ok = np.isfinite(a) & np.isfinite(b) & (a >= 0) & (b >= 0) & (den > 0)
    out[ok] = (a[ok] - b[ok]) / den[ok]
    return out


def write_tif(path, arrays, names, ul, res, win, dtype, nodata, tags):
    r0, _, c0, _ = win
    T = from_origin(ul[0] + c0 * res, ul[1] - r0 * res, res, res)
    h, w = arrays[0].shape
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=len(arrays), dtype=dtype,
                       crs=SINU, transform=T, nodata=nodata, compress="deflate") as ds:
        for i, (a, n) in enumerate(zip(arrays, names), 1):
            ds.write(a.astype(dtype), i)
            ds.set_band_description(i, n)
        ds.update_tags(**tags)
    return T


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", help="처리할 월(YYYY-MM). 생략 시 로컬에 있는 원본 전체. 다른 월의 기존 표 행은 유지")
    months = ap.parse_args().months
    man = pd.read_csv(ROOT / "tables/asset_manifest.csv", dtype=str, keep_default_na=False)
    h5s = man[(man.asset_kind == "data_h5") & (man.local_path != "")]
    if "local_retained" in h5s.columns:  # 로컬에서 내린 원본은 처리 대상 아님
        h5s = h5s[h5s.local_retained != "False"]
    if months:
        h5s = h5s[h5s.observation_time.str[:7].isin(months)]
    h5s = h5s.sort_values(["source_dataset", "observation_time"])
    if h5s.empty:
        print("처리할 granule 없음")
        return
    dongs = gpd.read_file(ROOT / CFG["aoi_file"]).to_crs(SINU)
    meta_cols = ["adm_cd2", "adm_cd", "adm_nm", "sggnm"]
    dong_area_km2 = dongs.set_index("adm_cd2").area / 1e6

    static = ROOT / "data/viirs/silver/static"
    static.mkdir(parents=True, exist_ok=True)
    with h5py.File(ROOT / h5s.local_path.iloc[0]) as f0:
        G = {}
        for k, gm in GRIDS.items():
            ul, res, n = grid_meta(f0, gm["group"])
            win = window_for(dongs.total_bounds, ul, res)
            wt = pixel_weights(dongs, ul, res, win)
            wt.to_csv(static / f"admin_pixel_weights_{k}.csv", index=False)
            G[k] = {"ul": ul, "res": res, "win": win, "wt": wt}
            print(k, "res", round(res, 4), "window", win, "dong-pixel pairs", len(wt))
    # 500m <-> 1km 정렬 확인: 같은 UL, res 2배
    assert np.allclose(G["500m"]["ul"], G["1km"]["ul"]) and np.isclose(G["1km"]["res"], 2 * G["500m"]["res"])

    idx_rows, obs_rows, op_rows = [], [], []
    for a in h5s.itertuples():
        prod = a.product_id.split(".")[0]
        date = a.observation_time[:10]
        obs_id = f"{prod}.002_{date.replace('-', '')}_{a.product_id.split('.')[-1]}_{PROCESSING_VERSION}"
        out = ROOT / f"data/viirs/{prod}/{obs_id}/silver"
        out.mkdir(parents=True, exist_ok=True)
        cands = overpass_candidates(prod, date)
        for s, e in cands:
            op_rows.append({"observation_id": obs_id, "source_dataset": a.source_dataset, "date": date,
                            "l1b_product": L1B[prod], "granule_start": s, "granule_end": e})
        win_start = cands[0][0] if cands else ""
        win_end = cands[-1][1] if cands else ""

        with h5py.File(ROOT / a.local_path) as f:
            g1 = f[f"HDFEOS/GRIDS/{GRIDS['1km']['group']}/Data Fields"]
            g5 = f[f"HDFEOS/GRIDS/{GRIDS['500m']['group']}/Data Fields"]
            w1, w5 = G["1km"]["win"], G["500m"]["win"]
            mb = ["M1", "M2", "M3", "M4", "M5", "M7", "M8", "M10", "M11"]
            M = {b: read(g1, f"SurfReflect_{b}_1", w1) for b in mb}
            QF = {i: read(g1, f"SurfReflect_QF{i}_1", w1) for i in range(1, 8)}
            lwm = read(g1, "land_water_mask_1", w1)
            ang = {k: read(g1, f"{k}_1", w1) for k in ("SensorZenith", "SolarZenith")}
            nob1 = read(g1, "num_observations_1km", w1)
            I = {b: read(g5, f"SurfReflect_{b}_1", w5) for b in ("I1", "I2", "I3")}
            iobs = read(g5, "iobs_res_1", w5)
            nob5 = read(g5, "num_observations_500m", w5)
            q_doc = {f"QF{i}": g1[f"SurfReflect_QF{i}_1"].attrs["QA index"].decode() for i in range(1, 8)}

        # ---- 1km 공통 QA mask
        def qa_mask(qf, fill):
            m = np.zeros(qf[1].shape, "uint16")
            m |= np.where(fill, M_FILL, 0).astype("uint16")
            m |= np.where(bit(qf[1], 4) == 1, M_NIGHT, 0).astype("uint16")
            cl = bit(qf[1], 2, 2)
            m |= np.where(cl >= 2, M_CLOUD, 0).astype("uint16")
            m |= np.where(cl == 1, M_PROBCLEAR, 0).astype("uint16")
            m |= np.where(bit(qf[2], 3) == 1, M_SHADOW, 0).astype("uint16")
            m |= np.where(bit(qf[2], 5) == 1, M_SNOW, 0).astype("uint16")
            m |= np.where(bit(qf[7], 1) == 1, M_ADJ, 0).astype("uint16")
            m |= np.where((bit(qf[2], 6) | bit(qf[2], 7) | bit(qf[7], 4)) == 1, M_CIRRUS, 0).astype("uint16")
            return m

        fill1 = (M["M4"] == FILL_SR) | (M["M10"] == FILL_SR) | (M["M7"] == FILL_SR) | (nob1 <= 0)
        mask1 = qa_mask(QF, fill1)
        # M4, M7, M10 품질: QF3 b3(M4) b5(M7) b7(M10) bad SDR; QF5 b5(M4) b7(M7) overall bad;
        # QF6 의 M10 bit 는 원천 문서에 'bit 2' 가 M11/M10 으로 중복 표기 -> M10 = bit1 로 가정(코드북 기재)
        bad1 = (bit(QF[3], 3) | bit(QF[3], 5) | bit(QF[3], 7)) == 1
        mask1 |= np.where(bad1, M_BADSDR, 0).astype("uint16")
        aot1 = (bit(QF[4], 4) | bit(QF[5], 5) | bit(QF[5], 7) | bit(QF[6], 1)) == 1
        mask1 |= np.where(aot1, M_AOTQ, 0).astype("uint16")

        # ---- 500m: 1km QF 를 2x2 nearest 로 대응, iobs_res_1 != 0 이면 다른 관측 -> 제외
        up = lambda x: np.repeat(np.repeat(x, 2, 0), 2, 1)
        r0o = w5[0] - 2 * w1[0]; c0o = w5[2] - 2 * w1[2]
        h5_, w5_ = I["I1"].shape
        QF5 = {i: up(QF[i])[r0o:r0o + h5_, c0o:c0o + w5_] for i in QF}
        fill5 = (I["I1"] == FILL_SR) | (I["I2"] == FILL_SR) | (I["I3"] == FILL_SR) | (nob5 <= 0)
        mask5 = qa_mask(QF5, fill5)
        bad5 = (bit(QF5[4], 1) | bit(QF5[4], 2) | bit(QF5[4], 3)) == 1
        mask5 |= np.where(bad5, M_BADSDR, 0).astype("uint16")
        aot5 = (bit(QF5[4], 4) | bit(QF5[6], 3) | bit(QF5[6], 4) | bit(QF5[6], 5)) == 1
        mask5 |= np.where(aot5, M_AOTQ, 0).astype("uint16")
        mask5 |= np.where(iobs != 0, M_QAMIS, 0).astype("uint16")

        # ---- 저장 (원천 정수값 그대로 + mask)
        tags = {"source_product_id": a.product_id, "source_sha256": a.sha256, "scale_factor_reflectance": str(SCALE),
                "fill_value_reflectance": str(FILL_SR), "mask_bits": "1 fill/no-obs,2 night,4 cloud,8 shadow,16 snow,32 bad SDR,64 500m qa-mismatch,128 adjacent cloud,256 thin cirrus,512 probably clear,1024 AOT/SR overall quality bad",
                "processing_version": PROCESSING_VERSION, "admin_boundary_version": BOUNDARY_VERSION}
        p5 = out / "admin_crop_500m.tif"
        T5 = write_tif(p5, [I["I1"], I["I2"], I["I3"], mask5.astype("int32")], ["I1", "I2", "I3", "mask_500m"],
                       G["500m"]["ul"], G["500m"]["res"], w5, "int32", None, tags)
        p1 = out / "admin_crop_1km.tif"
        l1 = [M[b] for b in mb] + [QF[i] for i in range(1, 8)] + [lwm, ang["SensorZenith"], ang["SolarZenith"], mask1]
        n1 = mb + [f"QF{i}" for i in range(1, 8)] + ["land_water_mask", "SensorZenith", "SolarZenith", "mask_1km"]
        T1 = write_tif(p1, [x.astype("int32") for x in l1], n1, G["1km"]["ul"], G["1km"]["res"], w1, "int32", None, tags)

        # ---- 픽셀 지수 (valid 에서만)
        refl = lambda x: np.where(x == FILL_SR, np.nan, x * SCALE)
        v5 = (mask5 & EXCLUDE) == 0
        v1 = (mask1 & EXCLUDE) == 0
        ndvi = np.where(v5, index_px(refl(I["I2"]), refl(I["I1"])), np.nan)
        ndwi_gao = np.where(v5, index_px(refl(I["I2"]), refl(I["I3"])), np.nan)
        mndwi = np.where(v1, index_px(refl(M["M4"]), refl(M["M10"])), np.nan)
        ndwi_mcf = np.where(v1, index_px(refl(M["M4"]), refl(M["M7"])), np.nan)

        # ---- 행정동 통계
        stats = {}
        for k, mask, cov_fill, ix in (("500m", mask5, fill5, {"ndvi_500m": ndvi, "ndwi_gao_500m": ndwi_gao}),
                                      ("1km", mask1, fill1, {"mndwi_1km": mndwi, "ndwi_mcfeeters_1km": ndwi_mcf})):
            wt = G[k]["wt"]
            rr, cc, ww = wt.row.values, wt.col.values, wt.weight.values
            mk = mask[rr, cc]
            cov = ~cov_fill[rr, cc]
            val = (mk & EXCLUDE) == 0
            vst = (mk & EXCLUDE_STRICT) == 0
            df = pd.DataFrame({"adm_cd2": wt.adm_cd2.values, "w": ww, "wc": ww * cov, "wv": ww * val,
                               "wcl": ww * (cov & ((mk & M_CLOUD) > 0)), "wsh": ww * (cov & ((mk & M_SHADOW) > 0)),
                               "wqm": ww * (cov & ((mk & M_QAMIS) > 0)), "wadj": ww * (cov & ((mk & M_ADJ) > 0)),
                               "wvs": ww * vst, "waot": ww * (cov & ((mk & M_AOTQ) > 0)),
                               "npx": 1, "npv": val.astype(int)})
            for name, arr in ix.items():
                vv = arr[rr, cc]
                df[name + "_num"] = np.where(np.isfinite(vv), vv * ww, 0.0)
                df[name + "_den"] = np.where(np.isfinite(vv), ww, 0.0)
            s = df.groupby("adm_cd2").sum(numeric_only=True)
            s_out = pd.DataFrame(index=s.index)
            s_out[f"total_px_{k}"] = s.w
            s_out[f"n_pixels_touched_{k}"] = s.npx
            s_out[f"coverage_fraction_{k}"] = s.wc / s.w
            s_out[f"valid_fraction_{k}"] = s.wv / s.w
            s_out[f"valid_px_{k}"] = s.wv
            s_out[f"valid_fraction_strict_{k}"] = s.wvs / s.w
            s_out[f"aot_quality_bad_fraction_{k}"] = (s.waot / s.wc).where(s.wc > 0)
            s_out[f"cloud_fraction_{k}"] = (s.wcl / s.wc).where(s.wc > 0)
            s_out[f"shadow_fraction_{k}"] = (s.wsh / s.wc).where(s.wc > 0)
            s_out[f"adjacent_cloud_fraction_{k}"] = (s.wadj / s.wc).where(s.wc > 0)
            if k == "500m":
                s_out["qa_mismatch_fraction_500m"] = (s.wqm / s.wc).where(s.wc > 0)
            for name in ix:
                s_out[name] = (s[name + "_num"] / s[name + "_den"]).where(s[name + "_den"] > 0)
            stats[k] = s_out
        st = stats["500m"].join(stats["1km"], how="outer")

        def reason(r):
            out_ = []
            for k, cols in (("500m", ["ndvi_500m", "ndwi_gao_500m"]), ("1km", ["mndwi_1km", "ndwi_mcfeeters_1km"])):
                if pd.isna(r[cols[0]]):
                    if r[f"coverage_fraction_{k}"] == 0:
                        out_.append(f"{k}: no observation (fill)")
                    else:
                        out_.append(f"{k}: no valid pixel (cloud {r[f'cloud_fraction_{k}']:.2f}, shadow {r[f'shadow_fraction_{k}']:.2f} of covered)")
            return "; ".join(out_)

        st["index_unavailable_reason"] = st.apply(reason, axis=1)
        st = st.reset_index().merge(dongs[meta_cols], on="adm_cd2", how="left")
        st["dong_area_km2"] = st.adm_cd2.map(dong_area_km2)
        base = {"observation_id": obs_id, "observation_time": a.observation_time,
                "observation_time_precision": "day", "overpass_window_start_utc": win_start,
                "overpass_window_end_utc": win_end, "n_daytime_overpass_candidates": len(cands),
                "source_product_ids": a.product_id, "source_dataset": a.source_dataset, "aoi_id": CFG["aoi_id"],
                "admin_boundary_version": BOUNDARY_VERSION,
                "processed_path": f"{p5.relative_to(ROOT)};{p1.relative_to(ROOT)}",
                "bands": "500m: I1,I2,I3 | 1km: M1,M2,M3,M4,M5,M7,M8,M10,M11,QF1-7,land_water_mask,SensorZenith,SolarZenith",
                "channel_order": "see band descriptions in tif", "pixel_resolution_m": f"500m grid {G['500m']['res']:.3f} | 1km grid {G['1km']['res']:.3f}",
                "source_resolution_m": "I-band 375 | M-band 750 (nadir)", "crs": SINU,
                "transform": f"500m {tuple(round(v, 6) for v in T5[:6])} | 1km {tuple(round(v, 6) for v in T1[:6])}",
                "qc_status": "sensor_qa_applied; visual_check_pending", "mask_policy": MASK_POLICY,
                "processing_version": PROCESSING_VERSION}
        for r in st.to_dict("records"):
            idx_rows.append(base | r)
        # 서울 전체(행정동 가중 합) 요약
        tot = {k: G[k]["wt"].weight.sum() for k in G}
        obs_rows.append(base | {
            "seoul_coverage_fraction_500m": (st.coverage_fraction_500m * st.total_px_500m).sum() / tot["500m"],
            "seoul_valid_fraction_500m": st.valid_px_500m.sum() / tot["500m"],
            "seoul_cloud_fraction_500m": (st.cloud_fraction_500m * st.coverage_fraction_500m * st.total_px_500m).sum()
                                         / (st.coverage_fraction_500m * st.total_px_500m).sum(),
            "seoul_valid_fraction_1km": st.valid_px_1km.sum() / tot["1km"],
            "n_dong_with_valid_500m": int((st.valid_px_500m > 0).sum()),
            "n_dong_with_valid_1km": int((st.valid_px_1km > 0).sum()),
            "n_dong_valid_ge_0.5_500m": int((st.valid_fraction_500m >= 0.5).sum()),
            "seoul_valid_fraction_strict_500m": (st.valid_fraction_strict_500m * st.total_px_500m).sum() / tot["500m"],
            "seoul_shadow_fraction_500m": (st.shadow_fraction_500m * st.coverage_fraction_500m * st.total_px_500m).sum()
                                          / (st.coverage_fraction_500m * st.total_px_500m).sum(),
            "dong_count": len(st)})
        (out / "crop_index.json").write_text(json.dumps(base | {
            "bronze_file": a.local_path, "bronze_sha256": a.sha256,
            "outputs": {p.name: sha256(p) for p in (p5, p1)},
            "window_rowcol": {k: G[k]["win"] for k in G},
            "grid_upper_left_m": G["1km"]["ul"], "qa_index_source_text": q_doc,
            "created_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=1))
        o = obs_rows[-1]
        print(obs_id, f"valid500 {o['seoul_valid_fraction_500m']:.2f} cloud500 {o['seoul_cloud_fraction_500m']:.2f} dongs>=50% {o['n_dong_valid_ge_0.5_500m']}", flush=True)

    done = {o["observation_id"] for o in obs_rows}
    for name, rows in (("silver_index.csv.gz", idx_rows), ("silver_observations.csv", obs_rows),
                       ("overpass_candidates.csv", op_rows)):
        path = ROOT / "tables" / name
        new = pd.DataFrame(rows)
        if path.exists():  # 이번에 처리하지 않은 관측(다른 월)의 기존 행은 유지
            old = pd.read_csv(path, dtype={"adm_cd2": str} if name.startswith("silver_index") else None)
            new = pd.concat([old[~old.observation_id.isin(done)], new], ignore_index=True)
        keys = [c for c in ("observation_id", "adm_cd2", "granule_start") if c in new.columns]
        new.sort_values(keys).to_csv(path, index=False)
        print(name, "rows", len(new))


if __name__ == "__main__":
    sys.exit(main())
