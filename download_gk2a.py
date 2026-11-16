import argparse
import os
import csv
import hashlib
import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

# -----------------------------
# 설정
# -----------------------------

START_DATE = "2024-01-01"
END_DATE = "2025-12-31"

# 00:00 UTC (09:00 KST) 일자별 대표 관측
INTERVAL_HOURS = 24

# 천리안 2A 설정
LVL = "le1b"
DATA = "ir112"
AREA = "ko"

# 동시 요청 수
MAX_WORKERS = 6

# 요청당 타임아웃
REQUEST_TIMEOUT = 120

# 실패 시 재시도 횟수
MAX_RETRIES = 2

# 저장 폴더
DOWNLOAD_DIR = Path("data/bronze/gk2a/IR112_KO")
LOG_DIR = DOWNLOAD_DIR / "logs"
MANIFEST_PATH = LOG_DIR / "asset_manifest.csv"
DOWNLOAD_LOG_PATH = LOG_DIR / "download_log.csv"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://apihub.kma.go.kr/api/typ05/api/GK2A"
NETCDF_SIGNATURES = (b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF\r\n\x1a\n")


def has_netcdf_signature(header: bytes) -> bool:
    return header.startswith(NETCDF_SIGNATURES)

# -----------------------------
# 유틸리티
# -----------------------------

def load_api_key():
    load_dotenv()
    key = os.getenv("KMA_API_KEY")

    if not key:
        raise ValueError(
            "KMA_API_KEY가 없습니다. .env 파일에 KMA_API_KEY=본인키 를 작성하세요."
        )

    return key


def sha256_of_file(path: Path) -> str:
    sha256 = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


def init_csv(path: Path, fieldnames: list):
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()


def append_csv(path: Path, row: dict, fieldnames: list):
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow(row)


def build_timestamps():
    start = datetime.strptime(START_DATE, "%Y-%m-%d")
    end = datetime.strptime(END_DATE, "%Y-%m-%d") + timedelta(days=1)

    timestamps = []
    current = start

    while current < end:
        timestamps.append(current)
        current += timedelta(hours=INTERVAL_HOURS)

    return timestamps


# -----------------------------
# 단일 다운로드
# -----------------------------

def download_one(dt_utc: datetime, api_key: str):
    tm = dt_utc.strftime("%Y%m%d%H%M")

    filename = f"gk2a_ami_{LVL}_{DATA}_{AREA}_{tm}.nc"
    output_path = DOWNLOAD_DIR / filename

    if output_path.is_file():
        with output_path.open("rb") as stream:
            existing_is_netcdf = has_netcdf_signature(stream.read(8))
    else:
        existing_is_netcdf = False
    if existing_is_netcdf:
        return {
            "status": "already_exists",
            "http_status": "",
            "file_size_bytes": output_path.stat().st_size,
            "sha256": sha256_of_file(output_path),
            "error_message": "",
        }

    url = f"{BASE_URL}/LE1B/{DATA.upper()}/{AREA.upper()}/data"
    params = {"date": tm, "authKey": api_key}

    last_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )

            if response.status_code in (401, 403):
                raise SystemExit(
                    f"HTTP {response.status_code}: API허브에서 천리안 2A호 "
                    "기본관측자료 다운로드 활용신청 및 인증키 권한을 확인하세요."
                )
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
                time.sleep(2 ** attempt)
                continue

            if not has_netcdf_signature(response.content[:8]):
                last_error = "HTTP 200 response has no NetCDF signature"
                break

            temporary_path = output_path.with_suffix(".nc.part")
            with temporary_path.open("wb") as f:
                f.write(response.content)
            temporary_path.replace(output_path)

            file_hash = sha256_of_file(output_path)

            return {
                "status": "success",
                "http_status": response.status_code,
                "file_size_bytes": len(response.content),
                "sha256": file_hash,
                "error_message": "",
            }

        except requests.exceptions.RequestException as e:
            last_error = type(e).__name__

        time.sleep(1)

    return {
        "status": "failed",
        "http_status": "",
        "file_size_bytes": 0,
        "sha256": "",
        "error_message": last_error,
    }


# -----------------------------
# 메인
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description="Collect GK2A IR112 KO Bronze NetCDF files.")
    parser.add_argument(
        "--replay-manifest", type=Path,
        help="Download the exact UTC observations listed in a Bronze asset manifest.",
    )
    args = parser.parse_args()
    expected_hashes = {}
    if args.replay_manifest:
        with args.replay_manifest.open(encoding="utf-8-sig", newline="") as stream:
            expected_hashes = {
                datetime.fromisoformat(row["observation_time_utc"]): row["sha256"]
                for row in csv.DictReader(stream)
            }
        timestamps = sorted(expected_hashes)
    else:
        timestamps = build_timestamps()
    if not timestamps:
        parser.error("No observation timestamps")

    api_key = load_api_key()

    manifest_fields = [
        "observation_time_utc",
        "local_filename",
        "file_path",
        "lvl",
        "data",
        "area",
        "file_size_bytes",
        "sha256",
        "download_status",
        "http_status",
        "error_message",
        "downloaded_at",
    ]

    log_fields = [
        "observation_time_utc",
        "status",
        "http_status",
        "file_size_bytes",
        "error_message",
        "attempted_at",
    ]

    init_csv(MANIFEST_PATH, manifest_fields)
    init_csv(DOWNLOAD_LOG_PATH, log_fields)

    print(f"총 요청 시각 수: {len(timestamps)}")
    print(f"동시 요청 수: {MAX_WORKERS}")
    print(f"저장 폴더: {DOWNLOAD_DIR}")
    print(f"로그: {DOWNLOAD_LOG_PATH}")
    print(f"매니페스트: {MANIFEST_PATH}")

    success_count = 0
    already_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_one, dt, api_key): dt
            for dt in timestamps
        }

        for future in as_completed(futures):
            dt_utc = futures[future]
            result = future.result()
            if dt_utc in expected_hashes and result["status"] != "failed":
                if result["sha256"] != expected_hashes[dt_utc]:
                    result.update(status="failed", error_message="SHA-256 differs from replay manifest")

            now = datetime.now().isoformat(timespec="seconds")

            log_row = {
                "observation_time_utc": dt_utc.isoformat(),
                "status": result["status"],
                "http_status": result["http_status"],
                "file_size_bytes": result["file_size_bytes"],
                "error_message": result["error_message"],
                "attempted_at": now,
            }

            append_csv(DOWNLOAD_LOG_PATH, log_row, log_fields)

            if result["status"] in ("success", "already_exists"):
                manifest_row = {
                    "observation_time_utc": dt_utc.isoformat(),
                    "local_filename": f"gk2a_ami_{LVL}_{DATA}_{AREA}_{dt_utc.strftime('%Y%m%d%H%M')}.nc",
                    "file_path": str(DOWNLOAD_DIR / f"gk2a_ami_{LVL}_{DATA}_{AREA}_{dt_utc.strftime('%Y%m%d%H%M')}.nc"),
                    "lvl": LVL,
                    "data": DATA,
                    "area": AREA,
                    "file_size_bytes": result["file_size_bytes"],
                    "sha256": result["sha256"],
                    "download_status": result["status"],
                    "http_status": result["http_status"],
                    "error_message": result["error_message"],
                    "downloaded_at": now,
                }

                append_csv(MANIFEST_PATH, manifest_row, manifest_fields)

            if result["status"] == "success":
                success_count += 1
            elif result["status"] == "already_exists":
                already_count += 1
            else:
                fail_count += 1

            print(
                f"{dt_utc.isoformat()} | "
                f"{result['status']} | "
                f"{result['file_size_bytes']} bytes"
            )

    print("완료")
    print(f"성공: {success_count}")
    print(f"이미 존재: {already_count}")
    print(f"실패: {fail_count}")
    return int(fail_count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
