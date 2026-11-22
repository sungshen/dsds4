# VIIRS 2024–2025 운영 가이드

운영 경로는 Hetzner `/root/viirs`의 `viirs_full_rebuild.py`와 Cloudflare R2 `viirs-daily`입니다. 플랫폼은 이 R2 결과를 조회합니다. 기간은 KST `[2024-01-01, 2026-01-01)`입니다.

## 처리 정책

CMR에서 Suomi NPP `VNP02IMG`·`VNP03IMG` Collection 2를 경도 124–132°, 위도 32–39°로 검색합니다. 시작 시각이 같은 영상·좌표 쌍을 **모두** 처리합니다. BBOX 교차가 한국 육지 관측을 보장하지는 않습니다.

과거 `--one-per-day`는 정오에 가까운 장면 하나를 선택했습니다. 2024-01-03에는 울릉도 287픽셀만 남았고 일부 날짜는 0행이었습니다. 이를 전국 일별 관측으로 해석하면 안 됩니다. 현재 방식은 모든 관측을 결합하고, 위치가 겹쳐도 다른 관측은 유지합니다.

1. Bronze: 원본 이름과 바이트를 보존합니다. 기존 R2 원본은 체크섬을 확인해 재사용하고 빠진 원본만 NASA에서 받습니다.
2. Silver: 원천 픽셀 중심을 연도별 전국 SGIS 행정동에 배정합니다. 보간·재격자화·다른 관측 간 mosaic을 하지 않습니다. 원천 행열·위경도·I05 밝기온도(K)·QA·장면·스캔 시각을 저장합니다.
3. Quality: 장면별 행 수, 유한 온도, 품질 제외·유효 수, 코드·원천 계보를 기록합니다. 구름 검증 완료를 뜻하지 않습니다.
4. Gold: 행정동 × 장면별 유효 온도 통계와 관측 시간을 저장합니다. 강수 정답과 폭우 라벨은 생성하지 않습니다.

2024년 `sgis-2024-2q`는 3,558개, 2025년 `sgis-2025-2q`는 3,559개 경계입니다. `ADM_CD`는 문자열입니다. SHP·SHX·DBF·PRJ·CPG를 함께 보관합니다. 6월 말 경계를 해당 연도 전체에 적용하는 정책이며 연중 개편을 자동 복원하지 않습니다.

## R2 경로

```text
viirs-daily/
├── bronze/YYYY/MM/DD/<VNP02IMG 또는 VNP03IMG 원본>.nc
├── silver/sgis-YYYY-2q/YYYY/MM/DD/all-observations.parquet
├── quality/sgis-YYYY-2q/YYYY/MM/DD/all-observations.json
└── gold/sgis-YYYY-2q/YYYY/MM/DD/dong-observations.parquet
```

새 실행기는 하루의 세 파일을 확인한 뒤 완료 표식을 게시합니다. 표식이 없거나 객체의 체크섬·ETag가 다르면 플랫폼은 게시 검증 완료로 표시하지 않습니다. 예전 결과도 조회할 수 있지만 최신 게시 검증과 구분합니다. [데이터 계약](docs/DATA_CONTRACT.md)을 참고하세요.

## 준비와 실행

```bash
cd /root/viirs
.venv/bin/python -m pip install -r requirements-viirs-s3.txt
systemctl list-units --all 'viirs*' --no-pager
```

서버 `.env`에는 `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_PREFIX=viirs-daily`를 둡니다. 새 다운로드에는 `EARTHDATA_TOKEN`이 필요합니다. 플랫폼은 읽기 전용 R2 권한으로 동작하며 서버는 원본 생성과 파생 결과 정리 권한도 필요합니다. 경계는 `boundaries/bnd_dong_00_2024_2Q.*`, `boundaries/bnd_dong_00_2025_2Q.*`입니다.

```bash
# 부분 실행: --stop은 제외. 운영 서비스와 동시에 실행하지 마세요.
.venv/bin/python -u viirs_full_rebuild.py --start 2024-01-03 --stop 2024-01-04
systemctl status viirs-full-rebuild --no-pager
journalctl -u viirs-full-rebuild -n 40 --no-pager
tail -n 12 full_rebuild_report/daily_results.csv
```

서비스 설치·재시작은 [운영 문서](docs/OPERATIONS.md)를 따릅니다. 동일 prefix에 구형 수집·Gold 작업을 동시에 실행하지 않습니다. 프로세스 잠금은 동일 staging 경로만 보호합니다. 다른 서버의 중복 쓰기까지 잠그지는 않습니다.

재실행은 입력 목록·경계·처리 코드 지문이 같은 날짜만 건너뜁니다. 코드·경계가 바뀌면 파생 결과를 재생성하며 Bronze는 덮어쓰지 않습니다. 여러 S3 객체가 하나의 트랜잭션이 되는 것은 아니므로 완료 표식과 객체의 일치 여부로 부분 게시를 감지합니다.

## 검수와 결측

```bash
# 읽기 전용 검사. --deep은 파생 객체 전체를 읽습니다.
.venv/bin/python viirs_r2_audit.py --out r2_audit_report
.venv/bin/python viirs_missing_repair.py --help
```

`NO_PAIRED_CMR`은 검색 조건에 맞는 쌍을 찾지 못했다는 뜻이며 NASA 전체 무관측의 증명은 아닙니다. 한 제품만 있으면 오류로 남깁니다. 행정동 안에 픽셀이 없으면 0행을 기록하고 미관측 값을 0 K로 만들지 않습니다.

HEAD 크기·메타데이터 검사와 전체 바이트의 해시 검증은 다릅니다. 과거 711일 `PASS`는 HEAD 검사이며 전국 coverage나 과학적 품질 완료를 뜻하지 않습니다. [과거 보고서](reports/2026-11-22-pre-rebuild/README.md)와 [현재 검수](docs/VERIFICATION.md)를 구분해 사용합니다.
