"""받은 국토위성 ARD zip 안의 메타데이터(_Aux.xml)를 표와 묶음 파일로 정리한다.

입력: tables/ngii/asset_manifest.csv (ngii_ingest_downloads.py 출력)
출력
- tables/ngii/metadata/ard_metadata_2025.csv : 영상 1개 = 1행 (도엽, 촬영일·UTC 시각, 궤도, 좌표 범위, 센서·태양 각도 등)
- tables/ngii/metadata/ard_aux_xml_2025.tar.gz : 원본 _Aux.xml 전체 (zip에서 꺼낸 그대로, 이름 유지)

zip은 풀지 않고 메모리에서 XML만 읽는다. 원본 zip은 data/ 아래에 그대로 둔다(git 제외).
"""
import csv
import io
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tables" / "ngii" / "asset_manifest.csv"
OUT_DIR = ROOT / "tables" / "ngii" / "metadata"
EVENT_PERIOD = {"E4": ("20250716", "20250720"), "E5": ("20250803", "20250803"),
                "E6": ("20250813", "20250813"), "E7": ("20250907", "20250907")}


def t(node, path):
    el = node.find(path) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else ""


def main() -> None:
    rows = list(csv.DictReader(MANIFEST.open(encoding="utf-8")))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    tar_path = OUT_DIR / "ard_aux_xml_2025.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for r in rows:
            zp = ROOT / r["stored_path"]
            with zipfile.ZipFile(zp) as z:
                name = next(n for n in z.namelist() if n.endswith("_Aux.xml"))
                data = z.read(name)
            info = tarfile.TarInfo(name=f"{r['event_id']}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

            root = ET.fromstring(data)
            g = root.find("General")
            sr1 = root.find("Image/SR1")
            inputs = root.findall(".//InputImages/InputImage")
            first = inputs[0] if inputs else None
            acq = t(g, "AcquisitionDate")
            s, e = EVENT_PERIOD.get(r["event_id"], ("", ""))
            phase = "사건 전" if acq < s else ("사건 후" if acq > e else "사건 중")
            out.append({
                "catalog_id": r["catalog_id"], "event_id": r["event_id"], "phase": phase,
                "map_number": t(g, "MapNumber"), "map_scale": t(g, "MapReferenceScale"),
                "acquisition_date": acq, "create_date": t(g, "CreateDate"),
                "imaging_center_utc": t(first, "ImagingTime/ImagingCenterTime/UTC"),
                "satellite": t(g, "Satellite"), "sensor": t(g, "Sensor"), "product_level": t(g, "ProductLevel"),
                "orbit_number": t(g, "OrbitNumber"), "orbit_direction": t(g, "OrbitDirection"), "pass_id": t(g, "PassID"),
                "crs_epsg": t(g, "CoordinateReferenceSystem/CSCode"),
                "gsd_m": t(sr1, "ImageGSD/Column"), "width": t(sr1, "ImageSize/Width"), "height": t(sr1, "ImageSize/Height"),
                "ul_lat": t(sr1, "ImagingCoordinates/UpperLeft/Latitude"), "ul_lon": t(sr1, "ImagingCoordinates/UpperLeft/Longitude"),
                "lr_lat": t(sr1, "ImagingCoordinates/LowerRight/Latitude"), "lr_lon": t(sr1, "ImagingCoordinates/LowerRight/Longitude"),
                "n_input_images": len(inputs), "input_l2g": ";".join(t(i, "ProductName") for i in inputs),
                "incidence_deg": t(first, "SensorAngle/Incidence"), "off_nadir_deg": t(first, "SensorAngle/OffNadir"),
                "sun_azimuth_deg": t(first, "SunAngle/Azimuth"), "sun_elevation_deg": t(first, "SunAngle/Elevation"),
                "atmospheric_correction": t(root, ".//AtmosphericCorrectionAlgorithm"),
                "aux_xml": name, "zip_sha256": r["sha256"],
            })
    csv_path = OUT_DIR / "ard_metadata_2025.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"{len(out)}행 -> {csv_path.relative_to(ROOT)}; XML {len(out)}개 -> {tar_path.relative_to(ROOT)} ({tar_path.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
