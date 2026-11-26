#!/usr/bin/env python3
"""Package existing handoff metadata and make private, expiring R2 GET links.

Does not collect or reprocess observations. Without --publish it writes only to
the ignored data/platform directory. --publish uploads a small share package;
it never changes existing observation objects or bucket access permissions.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/2026-11-26-data-handoff"
PRIVATE = ROOT / "data/platform/sharing"
KST = timezone(timedelta(hours=9))
TIERS = {"bronze", "silver", "gold", "quality", "publication"}
PUBLIC_FILES = (
    "README.md", "VIIRS.md", "docs/DATA_CONTRACT.md", "docs/VERIFICATION.md",
    "docs/OPERATIONS.md", "docs/DATA_HANDOFF.md", "docs/VARIABLES.md",
    "docs/폭우지역_행정동_전수목록_진행현황.md",
    "docs/tickets/NGII-01_2025_국토위성_원본_다운로드.md",
    "docs/tickets/NGII-02_2024_국토위성_원본_다운로드.md",
    "logs/gk2a_bronze_manifest.csv", "scripts/create_handoff_links.py",
    "scripts/gk2a_delivery_inventory.py", "scripts/ngii_delivery_inventory.py",
    "scripts/viirs_delivery_inventory.py", "scripts/viirs_scene_footprints.py", "scripts/verify_handoff_gold.py",
    "scripts/build_data_handoff.py", "crop_gk2a_silver.py", "register_crop.py",
    "download_gk2a.py", "viirs_pipeline.py", "viirs_s3.py", "viirs_r2_audit.py",
    "viirs-full-rebuild-hetzner/viirs_full_rebuild.py", "requirements-viirs.txt",
    "requirements-viirs-s3.txt", "requirements-r2-audit.txt",
)
REPORT_FILES = {
    "": ("README.md", "log-redaction.json", "summary.json", "dataset_catalog.csv", "SHA256SUMS.csv"),
    "gold": ("README.md", "gold_validation.csv", "gold_validation_summary.json"),
    "gk2a": ("README.md", "original_inventory.csv", "cleaned_observations.csv",
             "silver_raster_inventory.csv", "quality_summary.json"),
    "ngii": ("README.md", "image_inventory.csv", "image_event_links.csv", "band_metadata.csv",
             "provider_mask_metadata.csv", "event_summary.csv", "source_checksums.csv",
             "validation_findings.csv", "validation_summary.json", "variable_dictionary.csv"),
    "viirs": ("README.md", "objects.csv", "days.csv", "scenes.csv", "source_assets.csv", "issues.csv",
              "summary.json", "duplicate_candidates.json", "sample_head_checks.json",
              "latest_parquet_footers.json", "list_snapshot.json", "lineage_versions.json",
              "monthly_summary.csv", "scene_footprints.csv", "scene_footprints_summary.json",
              "audit_attempt_status.json", "server_daily_status.csv",
              "server_report_evidence.json"),
}
NGII_FILES = (
    "raw_ard_rows.csv", "raw_ard_grouped.txt", "asset_manifest.csv", "rain_region_dongs.csv",
    "event_summary.csv", "rain_region_coverage.csv", "raw_2024_supplement.csv", "ard_tiles_2024.csv",
    "dates_2024.csv", "dates_2025.csv", "dong_tile_coverage.csv", "sweep_sigungu.csv",
    "raw_ard_list_counts.csv", "event_summary_initial_aoi.csv", "search_results.csv",
    "event_registry.csv", "sweep_raw_2024.txt", "sweep_raw_2025.txt",
    "metadata/ard_metadata_2024.csv", "metadata/ard_metadata_2025.csv",
    "metadata/ard_aux_xml_2024.tar.gz", "metadata/ard_aux_xml_2025.tar.gz",
)
BEARER_MATERIAL = re.compile(
    rb"https?://[^\s\"'<>]*[?&](?:X-Amz-(?:Signature|Credential|Security-Token)|"
    rb"AWSAccessKeyId|Signature|authKey|api_key|access_token)=[^\s\"'<>]+"
    rb"|-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.IGNORECASE)


def key_parts(key):
    """Reject ambiguous paths before any prefix comparison or SDK call."""
    if (not isinstance(key, str) or not key or key != key.strip() or "\\" in key
            or any(ord(char) < 32 or ord(char) == 127 for char in key)):
        raise ValueError("Invalid object key")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Object keys must use a canonical relative path")
    decoded = unquote(key)
    if decoded != key:
        key_parts(decoded)
    return parts


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_handoff_report(inventory):
    """Require current successful sensor/Gold reports before generating links."""
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts/build_data_handoff.py"), "--check"],
                       cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Handoff reports are missing, failed or stale; rebuild and verify them before sharing") from None
    canonical = REPORT / "viirs/objects.csv"
    if not inventory.is_file() or not canonical.is_file() or sha(inventory) != sha(canonical):
        raise ValueError("Download inventory must match the current verified handoff inventory")


def read_objects(path, prefix):
    result = []
    seen = set()
    prefix_parts = key_parts(prefix)
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = row.get("object_key") or row.get("key") or row.get("Key")
            if not key:
                raise ValueError("Object inventory requires an object_key column")
            parts = key_parts(key)
            if parts[:len(prefix_parts)] != prefix_parts or len(parts) < len(prefix_parts) + 2:
                raise ValueError("Object inventory contains a key outside the observation prefix")
            tier = parts[len(prefix_parts)]
            if tier not in TIERS or (row.get("tier") and row["tier"] != tier):
                raise ValueError("Object inventory contains an unsupported tier")
            if key in seen:
                raise ValueError("Duplicate object key in inventory")
            size = int(row.get("size_bytes") or row.get("size") or row.get("Size") or 0)
            if size <= 0:
                raise ValueError("Cannot share a zero-sized or unknown-sized observation")
            seen.add(key)
            result.append({"dataset": "VIIRS", "tier": tier, "object_key": key,
                           "size_bytes": size, "sha256": row.get("sha256", ""),
                           "etag": row.get("etag", row.get("ETag", ""))})
    if not result:
        raise ValueError("Object inventory is empty")
    return result


def package_paths():
    # Explicit filenames: future backups, receipts, downloads or caches must not
    # silently enter a public package just because they sit beside safe reports.
    paths = [ROOT / name for name in PUBLIC_FILES]
    paths += [REPORT / folder / name for folder, names in REPORT_FILES.items() for name in names]
    paths += [ROOT / "tables/ngii" / name for name in NGII_FILES]
    paths += [ROOT / "data/silver/gk2a/IR112_KO" / name for name in
              ("observations.csv", "crop_index.csv", "observation_qc.csv.gz", "processing.json")]
    answer = []
    for p in sorted(set(paths)):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if (any(part.is_symlink() for part in [p, *p.parents] if part != ROOT and ROOT in part.parents)
                or not p.resolve().is_relative_to(ROOT.resolve())
                or p.name.startswith(".env") or "platform" in rel.parts):
            raise ValueError("A private or symlinked file was included in the share package")
        if p.name.endswith(".tar.gz"):
            with tarfile.open(p, "r:gz") as archive:
                for member in archive:
                    if (not member.isfile() or not re.fullmatch(r"E[1-7]/L3A_[^/]+_Aux\.xml", member.name)):
                        raise ValueError("Only known NGII XML metadata members may be packaged")
                    if BEARER_MATERIAL.search(archive.extractfile(member).read()):
                        raise ValueError("Credential material was found in package metadata")
        else:
            if p.suffix == ".gz":
                with gzip.open(p, "rb") as stream:
                    payload = stream.read()
            else:
                payload = p.read_bytes()
            if BEARER_MATERIAL.search(payload):
                raise ValueError("Credential material was found in package metadata")
        answer.append(p)
    return answer


def make_package(dest, revision):
    """Allowlisted metadata only; never archive the private backup directory."""
    paths = package_paths()
    checksums = [{"path": p.relative_to(ROOT).as_posix(), "size_bytes": p.stat().st_size,
                  "sha256": sha(p)} for p in paths]
    notice = ("DSDS 위성 자료 인계 패키지\n"
              f"저장소: https://github.com/sungshen/dsds4/tree/{revision}\n"
              "시작 문서: docs/DATA_HANDOFF.md\n"
              "이 묶음은 목록·메타데이터·기존 QC·처리 코드입니다. 원본 영상 전체가 아닙니다.\n"
              "VIIRS 실제 파일: 함께 제공되는 만료 시각 명시 다운로드 목록 이용\n"
              "GK2A 실제 파일: 위 공개 저장소 data/ 경로 이용\n"
              "NGII 원본은 현재 환경에 없어 다운로드 링크를 제공하지 못합니다.\n"
              "미검증 항목·행정경계 차이·공식 피해 라벨 미확보는 문서에 명시합니다.\n")
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in paths:
            z.write(p, p.relative_to(ROOT).as_posix())
        z.writestr("START_HERE.txt", notice)
        z.writestr("SHA256SUMS.json", json.dumps(checksums, ensure_ascii=False, indent=2) + "\n")
    dest.chmod(0o600)
    with zipfile.ZipFile(dest) as z:
        if z.testzip() is not None:
            raise ValueError("Metadata ZIP verification failed")
        for row in checksums:
            if hashlib.sha256(z.read(row["path"])).hexdigest() != row["sha256"]:
                raise ValueError("Metadata ZIP checksum mismatch")
    return len(checksums)


def signed_get(client, bucket, key, seconds):
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 604800:
        raise ValueError("Link lifetime must be between 1 second and 7 days")
    key_parts(key)
    endpoint = urlsplit(client.meta.endpoint_url)
    if (endpoint.scheme != "https" or not endpoint.hostname
            or not re.fullmatch(r"[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com", endpoint.hostname)
            or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise ValueError("Expected an HTTPS Cloudflare R2 account endpoint")
    url = client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                        ExpiresIn=seconds, HttpMethod="GET")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    expected_path = "/" + quote(bucket, safe="") + "/" + quote(key, safe="/")
    if parsed.hostname == bucket + "." + endpoint.hostname:
        expected_path = "/" + quote(key, safe="/")
    if (parsed.scheme != "https" or parsed.hostname not in (endpoint.hostname, bucket + "." + endpoint.hostname)
            or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment
            or parsed.path != expected_path or query.get("X-Amz-Expires") != [str(seconds)]
            or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
            or len(query.get("X-Amz-Date", [])) != 1
            or len(query.get("X-Amz-Credential", [])) != 1
            or len(query.get("X-Amz-Signature", [])) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", query["X-Amz-Signature"][0])):
        raise ValueError("Unexpected presigned URL configuration")
    start = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return url, start + timedelta(seconds=seconds)


def write_download_csv(path, rows):
    fields = ["dataset", "tier", "object_key", "size_bytes", "sha256", "etag", "download_url", "expires_at_kst"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    path.chmod(0o600)


def upload_share_file(client, bucket, key, path, content_type):
    """Write only new handoff objects, not source or derived observations."""
    parts = key_parts(key)
    if parts[0] != "handoff" or len(parts) < 3:
        raise ValueError("Share uploads are restricted to handoff/")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    md5 = base64.b64encode(hashlib.md5(payload).digest()).decode()
    with io.BytesIO(payload) as f:
        client.put_object(Bucket=bucket, Key=key, Body=f, ContentLength=len(payload),
                          ContentType=content_type, ContentMD5=md5, IfNoneMatch="*",
                          Metadata={"sha256": digest, "purpose": "dsds-data-handoff"})
    head = client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != len(payload) or head["Metadata"].get("sha256") != digest:
        raise ValueError("Share upload failed metadata verification")


def index_html(rows, package_url, csv_url, expiry, revision, published):
    # URLs are bearer links. This HTML is private and excluded from Git.
    payload = json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")
    e = html.escape
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>DSDS 위성 자료 공유</title><style>
body{{font:16px/1.6 system-ui,sans-serif;max-width:1180px;margin:40px auto;padding:0 24px;color:#172942;background:#f6f8fc}}
h1{{font-size:32px}}.card{{background:white;border:1px solid #dfe5ef;border-radius:12px;padding:24px;margin:20px 0}}
a{{color:#075cb9}}.button{{display:inline-block;padding:10px 16px;border-radius:7px;background:#075cb9;color:white;text-decoration:none;margin:6px 10px 6px 0}}
input,select,button{{font:inherit;padding:9px;border:1px solid #b8c6d9;border-radius:6px;max-width:100%}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e6eaf1;word-break:break-word}}
.muted{{color:#526780}}.warning{{border-left:4px solid #df9b1b;padding-left:15px}}.scroll{{overflow:auto}}code{{font-size:13px}}
</style><h1>DSDS 위성 자료 공유</h1><p>2024–2025 관측 자료 · 기존 원본과 정제 결과를 그대로 공유합니다.</p>
<div class="card"><b>다운로드 링크 만료: {e(expiry.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S KST'))}</b>
<p>API 키나 별도 로그인 없이 받을 수 있습니다. 링크를 가진 사람이 접근할 수 있으므로 수업 관계자에게만 전달하세요.</p>
<a class="button" href="{e(package_url)}">자료 안내·변수 정의·목록 ZIP</a><a class="button" href="{e(csv_url)}">VIIRS 다운로드 목록 CSV</a>
<p><a href="https://github.com/sungshen/dsds4/tree/{e(revision)}">GitHub 코드·문서·GK2A 파일 열기</a></p></div>
<div class="card"><h2>자료별 상태</h2><ul><li>VIIRS: 아래 목록의 기존 게시 파일을 제공합니다. 검색 조건에 맞는 관측쌍이 없는 날짜와 검증 범위는 ZIP 안 날짜별 보고서를 확인하세요.</li>
<li>GK2A: 기존 원본과 일별 대표 관측·서울 행정동 raster·mask는 GitHub에서 받을 수 있습니다. 값은 원시 DN입니다.</li>
<li>NGII: 원본 관리 이력·XML 메타데이터를 제공합니다. 현재 환경에는 영상 원본이 없어 이 링크로 영상 파일을 제공하지 못합니다.</li></ul>
<p class="warning">구름 QC 완료·공식 피해 규모 정답 확보를 뜻하지 않습니다. 센서별 시간·단위·경계 버전이 다릅니다. 자세한 제한은 ZIP 안 docs/DATA_HANDOFF.md와 docs/VARIABLES.md를 확인하세요.</p></div>
<div class="card"><h2>VIIRS 파일 찾기</h2><p>원본 NetCDF는 용량이 큽니다. 필요한 날짜와 종류를 골라 받으세요.</p>
<select id="tier"><option value="gold">Gold · 행정동 관측 통계</option value="silver">Silver · 실제 픽셀</option value="quality">Quality · 품질·계보</option value="publication">완료 표식</option value="bronze">Bronze · 원본 영상·좌표</option value="">모든 종류</option></select>
<input id="query" placeholder="날짜(2024/07/08), 장면 ID 검색" size="34"><p id="count" class="muted"></p>
<div class="scroll"><table><thead><tr><th>종류</th><th>파일</th><th>크기</th><th>받기</th></tr></thead><tbody id="files"></tbody></table></div><button id="more">100개 더 보기</button></div>
<script type="application/json" id="data">{payload}</script><script>
const rows=JSON.parse(document.getElementById('data').textContent);let limit=100;
function render(){{const tier=document.getElementById('tier').value,q=document.getElementById('query').value.toLowerCase();
const result=rows.filter(r=>(!tier||r.tier===tier)&&r.object_key.toLowerCase().includes(q));
document.getElementById('count').textContent=`${{result.length.toLocaleString()}}개 · ${{(result.reduce((n,r)=>n+Number(r.size_bytes),0)/1073741824).toFixed(2)}} GiB · ${{Math.min(limit,result.length)}}개 표시`;
const body=document.getElementById('files');body.replaceChildren();for(const row of result.slice(0,limit)){{const tr=document.createElement('tr');
for(const value of [row.tier,row.object_key,(Number(row.size_bytes)/1048576).toFixed(2)+' MiB']){{const td=document.createElement('td');td.textContent=value;tr.appendChild(td)}}
const td=document.createElement('td'),a=document.createElement('a');a.href=row.download_url;a.textContent='다운로드';a.rel='noreferrer';td.appendChild(a);tr.appendChild(td);body.appendChild(tr)}}
document.getElementById('more').hidden=result.length<=limit}}
for(const id of ['tier','query'])document.getElementById(id).addEventListener('input',()=>{{limit=100;render()}});
document.getElementById('more').onclick=()=>{{limit+=100;render()}};render();</script></html>'''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", type=Path, default=REPORT / "viirs/objects.csv")
    p.add_argument("--expires-seconds", type=int, default=604800)
    p.add_argument("--publish", action="store_true", help="upload only the share package, CSV and index to private R2")
    args = p.parse_args(argv)
    if not 1 <= args.expires_seconds <= 604800:
        p.error("--expires-seconds must be between 1 and 604800")
    verify_handoff_report(args.inventory)
    # Explicit values: do not let empty platform examples override the root .env.
    from dotenv import dotenv_values
    import boto3
    from botocore.config import Config
    cfg = {}
    for layer in (dotenv_values(ROOT / ".env"), dotenv_values(ROOT / "platform/.env"), os.environ):
        cfg.update({k: v for k, v in layer.items() if v and str(v).strip()})
    required = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not all(cfg.get(k) for k in required):
        raise ValueError("R2 configuration is incomplete")
    prefix = cfg.get("R2_PREFIX", "viirs-daily").strip("/")
    rows = read_objects(args.inventory, prefix)
    client = boto3.client("s3", endpoint_url=f'https://{cfg["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
                          aws_access_key_id=cfg["R2_ACCESS_KEY_ID"], aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
                          region_name="auto", config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=60,
                          request_checksum_calculation="when_required", response_checksum_validation="when_required"))
    bucket = cfg["R2_BUCKET"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = PRIVATE / stamp
    dest.mkdir(parents=True, exist_ok=False)
    dest.chmod(0o700)
    package = dest / "dsds-data-handoff.zip"
    count = make_package(package, revision)
    expiries = []
    for row in rows:
        url, expiry = signed_get(client, bucket, row["object_key"], args.expires_seconds)
        row.update(download_url=url, expires_at_kst=expiry.astimezone(KST).isoformat())
        expiries.append(expiry)
    csv_file = dest / "viirs-downloads.csv"
    write_download_csv(csv_file, rows)
    share_prefix = f"handoff/{stamp}-{revision[:8]}"
    package_key, csv_key = share_prefix + "/" + package.name, share_prefix + "/" + csv_file.name
    if args.publish:
        upload_share_file(client, bucket, package_key, package, "application/zip")
        upload_share_file(client, bucket, csv_key, csv_file, "text/csv; charset=utf-8")
        package_url, exp = signed_get(client, bucket, package_key, args.expires_seconds)
        expiries.append(exp)
        csv_url, exp = signed_get(client, bucket, csv_key, args.expires_seconds)
        expiries.append(exp)
    else:
        package_url, csv_url = package.name, csv_file.name
    earliest = min(expiries)
    index = dest / "index.html"
    index.write_text(index_html(rows, package_url, csv_url, earliest, revision, args.publish), encoding="utf-8")
    index.chmod(0o600)
    index_url = ""
    if args.publish:
        index_key = share_prefix + "/index.html"
        upload_share_file(client, bucket, index_key, index, "text/html; charset=utf-8")
        index_url, _ = signed_get(client, bucket, index_key, args.expires_seconds)
    receipt = {"git_commit": revision, "published": args.publish, "expires_at_kst": earliest.astimezone(KST).isoformat(),
               "download_objects": len(rows), "package_files": count, "package_bytes": package.stat().st_size,
               "package_sha256": sha(package), "index_url": index_url, "package_url": package_url,
               "downloads_csv_url": csv_url, "observation_objects_changed": False, "bucket_access_changed": False,
               "private_directory": str(dest.relative_to(ROOT))}
    receipt_path = dest / "share-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    receipt_path.chmod(0o600)
    # Avoid leaking bearer links into terminal logs or tracked reports.
    print(json.dumps({k: receipt[k] for k in ("published", "expires_at_kst", "download_objects", "package_files", "package_bytes", "private_directory")}, ensure_ascii=False))
    return receipt_path


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # SDK exception URLs may contain signatures. Do not log exception bodies.
        print(f"Share preparation failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
