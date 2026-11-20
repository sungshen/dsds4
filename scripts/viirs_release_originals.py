"""디스크 공간 확보용: Silver 처리가 끝난 월의 원본 .h5 를 로컬에서 내린다(삭제). .cmr.xml 과 sha256 기록은 유지.

asset_manifest 에 local_retained=False, released_at, release_reason 을 기록한다. 원본은 viirs_download.py 로 재다운로드 가능
(재다운로드 시 sha256 이 manifest 값과 같은지 viirs_bronze_check.py 로 확인).
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="+", required=True)
    ap.add_argument("--reason", default="작업 환경 디스크 한도(클라우드 세션)로 Silver 처리·검증 후 로컬 원본 삭제; 재다운로드 가능")
    a = ap.parse_args()
    mp = ROOT / "tables/asset_manifest.csv"
    man = pd.read_csv(mp, dtype=str, keep_default_na=False)
    for c in ("local_retained", "released_at", "release_reason"):
        if c not in man.columns:
            man[c] = ""
    sil = {"_".join(p.parent.parent.name.split("_")[:2]) for p in (ROOT / "data/viirs").glob("*/*/silver/crop_index.json")}
    sel = (man.asset_kind == "data_h5") & man.observation_time.str[:7].isin(a.months) & (man.local_retained != "False")
    freed = 0
    for i in man.index[sel]:
        r = man.loc[i]
        key = f"{r.product_id.split('.')[0]}.002_{r.observation_time[:10].replace('-', '')}"
        if key not in sil:
            print("Silver 미처리, 건너뜀:", r.product_id)
            continue
        p = ROOT / r.local_path
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
        man.loc[i, ["local_retained", "released_at", "release_reason"]] = [
            "False", datetime.now(timezone.utc).isoformat(timespec="seconds"), a.reason]
    man.loc[(man.local_retained == "") & (man.local_path != ""), "local_retained"] = "True"
    man.to_csv(mp, index=False)
    print(f"released {int(sel.sum())} .h5, freed {freed / 1e9:.1f} GB")


if __name__ == "__main__":
    main()
