"""국토정보맵에서 받은 국토위성 원본(zip 등)을 bronze 폴더로 옮기고 관리대장(asset_manifest)을 기록한다.

사용:
    python -I scripts/ngii_ingest_downloads.py <받은 폴더> <event_id> [--dry-run]

규칙
- 파일 이름에서 카탈로그 ID(L3A_..., C1_..._L0F)를 찾아 data/ngii/2025/<event_id>/bronze/originals/<catalog_id>/ 로 옮긴다.
- 이름 변경·압축 해제를 하지 않는다. 같은 이름이 이미 있으면 sha256이 같을 때만 건너뛰고, 다르면 멈춘다.
- ID를 못 찾은 파일(여러 영상을 묶은 zip 등)은 data/ngii/2025/<event_id>/bronze/originals/_batch/ 로 옮기고 manifest에 catalog_id를 비워 둔다.
- tables/ngii/asset_manifest.csv 에 한 줄씩 추가한다.
"""
import csv
import hashlib
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tables" / "ngii" / "asset_manifest.csv"
FIELDS = ["catalog_id", "event_id", "product_type", "capture_date", "file_name", "size_bytes",
          "sha256", "stored_path", "source_path", "ingested_at_kst"]
ID_RE = re.compile(r"(L3A_\d{12}_\d{8}_\d{8}|C1_\d{14}_\d+_\d+_L0F)")
KST = timezone(timedelta(hours=9))


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(src: Path, event_id: str, dry: bool) -> None:
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in {".zip", ".xml", ".xlsx", ".xls", ".tif", ".tiff"})
    if not files:
        print("옮길 파일 없음:", src)
        return
    new_rows = []
    for p in files:
        m = ID_RE.search(p.name)
        cid = m.group(1) if m else ""
        ptype = "ARD" if cid.startswith("L3A") else "정사영상" if cid.startswith("C1") else ""
        cdate = (cid.split("_")[2] if ptype == "ARD" else cid.split("_")[1][:8]) if cid else ""
        dest_dir = ROOT / "data" / "ngii" / "2025" / event_id / "bronze" / "originals" / (cid or "_batch")
        dest = dest_dir / p.name
        digest = sha256(p)
        if dest.exists():
            if sha256(dest) == digest:
                print("이미 있음(동일):", dest.relative_to(ROOT))
                continue
            sys.exit(f"같은 이름, 다른 내용: {dest} — 멈춤")
        print(("[dry] " if dry else "") + f"{p.name} -> {dest.relative_to(ROOT)}")
        if not dry:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), dest)
        new_rows.append({"catalog_id": cid, "event_id": event_id, "product_type": ptype,
                         "capture_date": cdate, "file_name": p.name, "size_bytes": dest.stat().st_size if not dry else p.stat().st_size,
                         "sha256": digest, "stored_path": str(dest.relative_to(ROOT)), "source_path": str(p).replace(str(Path.home()), "~", 1),
                         "ingested_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")})
    if dry or not new_rows:
        return
    write_header = not MANIFEST.exists()
    with MANIFEST.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new_rows)
    print(f"manifest {len(new_rows)}행 추가: {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 2:
        sys.exit(__doc__)
    main(Path(args[0]).expanduser(), args[1], "--dry-run" in sys.argv)
