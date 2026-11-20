"""search_results 중 지정 월(config download_months)의 granule 원본(.h5)+metadata(.cmr.xml)를 Bronze 에 그대로 저장.

- 파일명·내용 변경 없음, 압축해제 없음.
- 인증: 실행 환경이 data.lpdaac.earthdatacloud.nasa.gov 요청에 Earthdata 토큰을 붙인다
  (클라우드: 네트워크 프록시가 주입 / 로컬: ~/.netrc 또는 환경변수 EARTHDATA_TOKEN). 토큰은 로그·표에 남기지 않는다.
- 실패해도 멈추지 않고 사유를 기록한다. 재실행 시 기존 파일은 HDF5 서명·크기 확인 후 skip.
- 결과: tables/asset_manifest.csv (asset 1개 = 1행), tables/download_log.csv (시도 1회 = 1행),
        tables/search_results.csv 의 download_status/failure_reason 갱신.
"""
import argparse, csv, hashlib, json, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/viirs.json").read_text())
try:  # 로컬 실행: 프로젝트 폴더의 .env 에서 EARTHDATA_TOKEN 을 읽는다 (값은 출력하지 않음)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
NATIVE = {"500m_grid": "463.313 (grid); 375 nadir I-band", "1km_grid": "926.625 (grid); 750 nadir M-band"}
SINU = "+proj=sinu +R=6371007.181 +nadgrids=@null +wktext +units=m +no_defs"
HDF5_SIG = b"\x89HDF\r\n\x1a\n"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def session():
    s = requests.Session()
    tok = os.getenv("EARTHDATA_TOKEN")
    if tok:  # 로컬 실행용. 값은 출력하지 않는다.
        s.headers["Authorization"] = f"Bearer {tok}"
    return s


def fetch(s, url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = ""
    for attempt in range(1, 4):
        t0 = now()
        try:
            with s.get(url, stream=True, timeout=(30, 600), allow_redirects=True) as r:
                code = r.status_code
                ctype = r.headers.get("Content-Type", "")
                if code != 200:
                    last = f"HTTP {code}"
                else:
                    with open(tmp, "wb") as f:
                        for c in r.iter_content(1 << 20):
                            f.write(c)
                    head = tmp.open("rb").read(8)
                    if dest.suffix == ".h5" and head != HDF5_SIG:
                        last = f"not HDF5 (Content-Type={ctype}, first bytes={head!r})"
                    elif dest.suffix == ".xml" and not head.startswith(b"<"):
                        last = f"not XML (Content-Type={ctype})"
                    else:
                        tmp.replace(dest)
                        return {"status": "downloaded", "http_status": code, "error": "",
                                "started": t0, "finished": now(), "attempt": attempt}
        except requests.RequestException as e:
            code, last = "", f"{type(e).__name__}: {str(e)[:150]}"
        tmp.unlink(missing_ok=True)
        yield_row = {"status": "failed", "http_status": code, "error": last,
                     "started": t0, "finished": now(), "attempt": attempt}
        LOG.append(yield_row | {"url": url})
        time.sleep(5 * attempt)
    return {"status": "failed", "http_status": code, "error": last, "started": t0, "finished": now(), "attempt": 3}


LOG = []


def one(row):
    prod = row.product_id.split(".")[0]
    month = row.observation_time[:7]
    odir = ROOT / f"data/viirs/{prod}/{month}/bronze/originals/{row.product_id}"
    odir.mkdir(parents=True, exist_ok=True)
    s = session()
    out = []
    for kind, url in (("data_h5", row.source_url), ("metadata_cmr_xml", row.source_url[:-3] + ".cmr.xml")):
        dest = odir / url.rsplit("/", 1)[1]
        if dest.exists() and dest.stat().st_size > 0 and dest.open("rb").read(8)[:1] in (b"\x89", b"<"):
            res = {"status": "already_exists", "http_status": "", "error": "", "started": "", "finished": "", "attempt": 0}
        else:
            res = fetch(s, url, dest)
        LOG.append(res | {"url": url})
        ok = dest.exists() and res["status"] in ("downloaded", "already_exists")
        out.append({
            "source_dataset": row.source_dataset, "product_id": row.product_id, "asset_kind": kind,
            "band_name": "all (HDF-EOS5 grids VIIRS_Grid_500m_2D, VIIRS_Grid_1km_2D)" if kind == "data_h5" else "",
            "source_url": url,
            "native_resolution_m": f"{NATIVE['500m_grid']} | {NATIVE['1km_grid']}" if kind == "data_h5" else "",
            "native_crs": SINU if kind == "data_h5" else "",
            "observation_time": row.observation_time, "observation_time_end": row.observation_time_end,
            "download_started_at": res["started"], "download_finished_at": res["finished"],
            "collected_at": now() if ok else "",
            "local_path": str(dest.relative_to(ROOT)) if ok else "",
            "sha256": sha256(dest) if ok else "", "file_size_bytes": dest.stat().st_size if ok else "",
            "cmr_granule_size_mb": row.granule_size_mb if kind == "data_h5" else "",
            "download_status": res["status"], "failure_reason": res["error"]})
    print(row.product_id, [o["download_status"] for o in out], flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", help="예: 2024-08 2024-09 (생략 시 config download_months 전체)")
    args = ap.parse_args()
    months = args.months or CFG["download_months"]
    if not os.getenv("EARTHDATA_TOKEN") and not (Path.home() / ".netrc").exists():
        print("주의: EARTHDATA_TOKEN 이 없습니다. .env 에 EARTHDATA_TOKEN=발급토큰 을 넣으세요. (클라우드 프록시 환경이면 무시)")
    sr_path = ROOT / "tables/search_results.csv"
    sr = pd.read_csv(sr_path, dtype=str, keep_default_na=False)
    sel = sr[(sr.in_period == "True") & sr.observation_time.str[:7].isin(months)]
    # 동일 product_id 가 여러 검색(월 경계)에 나타날 수 있으므로 다운로드만 1회
    todo = sel.drop_duplicates("product_id")
    print("to download:", len(todo))
    with ThreadPoolExecutor(4) as ex:
        rows = [r for rs in ex.map(one, todo.itertuples()) for r in rs]
    man = ROOT / "tables/asset_manifest.csv"
    old = pd.read_csv(man, dtype=str, keep_default_na=False) if man.exists() else pd.DataFrame()
    new = pd.DataFrame(rows)
    if len(old):  # 재실행: 같은 asset 은 새 기록으로 교체(already_exists 면 기존 collected_at 유지)
        keep = old.set_index(["product_id", "asset_kind"])
        for i, r in new.iterrows():
            k = (r.product_id, r.asset_kind)
            if r.download_status == "already_exists" and k in keep.index:
                for c in ("collected_at", "download_started_at", "download_finished_at"):
                    new.at[i, c] = keep.loc[k, c]
        old = old[~old.set_index(["product_id", "asset_kind"]).index.isin(new.set_index(["product_id", "asset_kind"]).index)]
    pd.concat([old, new]).to_csv(man, index=False)
    pd.DataFrame(LOG).to_csv(ROOT / "tables/download_log.csv", mode="a", index=False,
                             header=not (ROOT / "tables/download_log.csv").exists())
    st = new[new.asset_kind == "data_h5"].set_index("product_id")
    m = sr.product_id.isin(st.index) & (sr.in_period == "True")
    sr.loc[m, "download_status"] = sr.loc[m, "product_id"].map(
        lambda p: "downloaded" if st.loc[p, "download_status"] in ("downloaded", "already_exists") else "failed")
    sr.loc[m, "failure_reason"] = sr.loc[m, "product_id"].map(lambda p: st.loc[p, "failure_reason"])
    sr.to_csv(sr_path, index=False)
    print(new.groupby(["asset_kind", "download_status"]).size())


if __name__ == "__main__":
    main()
