"""Bronze observation_calendar: source_dataset x 날짜(UTC) 1행. 2024-01-01 ~ 2025-12-31 전체.

결측일(L2G granule 0건)은 같은 위성의 L1B(VxxO2MOD) 존재 여부를 CMR 로 조회해 원인을 구분한다.
  - L1B 도 0건  -> no_acquisition_at_source (위성/센서 자료 자체 없음)
  - L1B 는 있음 -> l2g_not_published (원천 관측은 있으나 L2G 제품이 CMR 에 없음)
usable_count 는 Silver QC 전이므로 비워 둔다(0 아님).
"""
import json
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/viirs.json").read_text())
L1B = {"VNP09GA.002": "VNP02MOD", "VJ109GA.002": "VJ102MOD", "VJ209GA.002": "VJ202MOD"}
BBOX = "126.764278,37.429011,127.183552,37.701080"


def l1b_hits(short, date):
    r = requests.get(CFG["cmr_url"], params={"short_name": short, "temporal": f"{date}T00:00:00Z,{date}T23:59:59Z",
                                              "bounding_box": BBOX, "page_size": 1}, timeout=60)
    r.raise_for_status()
    return int(r.headers["CMR-Hits"])


def main():
    sr = pd.read_csv(ROOT / "tables/search_results.csv", dtype=str, keep_default_na=False)
    sr = sr[sr.in_period == "True"].copy()
    sr["date"] = sr.observation_time.str[:10]
    man = pd.read_csv(ROOT / "tables/asset_manifest.csv", dtype=str, keep_default_na=False)
    ok = set(man[(man.asset_kind == "data_h5") & man.download_status.isin(["downloaded", "already_exists"])].product_id)
    dates = pd.date_range("2024-01-01", "2025-12-31").strftime("%Y-%m-%d")
    rows = []
    for ds in [f"{p['short_name']}.{p['version']}" for p in CFG["products"]]:
        g = sr[sr.source_dataset == ds].groupby("date")
        for d in dates:
            pids = list(g.get_group(d).product_id) if d in g.groups else []
            n_dl = sum(p in ok for p in pids)
            in_dl_scope = d[:7] in CFG["download_months"]
            if not pids:
                h = l1b_hits(L1B[ds], d)
                status = "no_acquisition_at_source" if h == 0 else "l2g_not_published"
                reason = (f"CMR 에 L2G granule 0건; {L1B[ds]} L1B {h}건" +
                          (" -> 위성/센서 원천 자료 없음(촬영 없음)" if h == 0 else " -> 원천은 있으나 L2G 미공개(공개 미확보)"))
            elif not in_dl_scope:
                status, reason = "searched_not_downloaded", "다운로드 범위 밖(현재 download_months 만 수집)"
            elif n_dl < len(pids):
                status, reason = "download_failed", "asset_manifest 참고"
            else:
                status, reason = "downloaded_qc_pending", "Silver 품질검증 전(usable_count 미산출)"
            if len(pids) > 1:
                reason += f"; 동일 날짜 granule {len(pids)}개(재처리 버전 추정, 생산시각 상이) - 제외하지 않고 모두 보존"
            rows.append({"source_dataset": ds, "aoi_id": CFG["aoi_id"], "date": d, "acquisition_count": len(pids),
                         "downloaded_count": n_dl if in_dl_scope else "", "usable_count": "",
                         "status": status, "failure_reason": reason, "product_ids": ";".join(pids)})
    out = pd.DataFrame(rows)
    out.to_csv(ROOT / "tables/observation_calendar.csv", index=False)
    print(out.groupby(["source_dataset", "status"]).size())


if __name__ == "__main__":
    main()
