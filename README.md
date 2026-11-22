# DSDS 관측 데이터

2024-01-01부터 2025-12-31까지의 위성 관측과 행정동 자료를 관리합니다. 날짜는 KST이며 명령의 `--stop`은 종료일을 포함하지 않습니다. 전체 기간의 종료 인자는 `2026-01-01`입니다.

## 데이터별 현재 구성

| 자료 | 원본·범위 | Silver | Gold·검증 상태 |
|---|---|---|---|
| VIIRS I05 | Suomi NPP Collection 2, 한국 BBOX와 교차하는 모든 영상·좌표 쌍, Hetzner → R2 | 날짜별 `all-observations.parquet`, 전국 SGIS 행정동 코드·원천 행열·관측 시각 | `dong-observations.parquet`의 행정동 × 관측 통계. 전체 기간 재구축 진행 중 |
| 천리안 GK2A IR112 | 일별 대표 731회, 한반도 KO 2 km 격자 | 서울 425개 행정동별 raster·mask, 관측은 TIFF의 731개 band | 원시 DN. 온도 변환·구름 QC·강수 라벨 미완료 |
| 국토위성 NGII ARD | 2024 E1·E3, 2025 E4·E6 다운로드 이력·메타데이터 | 관리대장만으로 자체 Silver 검증 완료를 뜻하지 않음 | 대용량 원본은 Git 제외. 재현 근거는 `tables/ngii/`와 다운로드 티켓 |

VIIRS 파일 하나에는 여러 행정동·관측이 들어갑니다. GK2A는 행정동별 TIFF 방식입니다. VIIRS 375 m급 제품과 GK2A IR112 2 km 격자는 고해상도 지상 사진이 아닙니다.

## 플랫폼 실행

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r platform/requirements.txt
cp .env.example .env   # 기존 .env가 있으면 덮어쓰지 마세요
# .env에 R2와 필요한 SSH 설정 입력
PYTHONPATH=platform .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

브라우저에서 <http://127.0.0.1:8765>를 엽니다. Gold는 R2의 실제 산출물을 읽습니다. PostgreSQL 설치와 SQL 연결은 필요하지 않습니다. 과거 로컬 SQLite의 7,108행은 전체 Gold 건수가 아닙니다.

## 코드와 문서

| 목적 | 위치 |
|---|---|
| VIIRS 운영 수집·재개 | [VIIRS.md](VIIRS.md), [통합 실행기](viirs-full-rebuild-hetzner/viirs_full_rebuild.py) |
| 플랫폼 사용·설정 | [platform/README.md](platform/README.md) |
| 서버 운영·장애 대응 | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Silver·Gold 단위와 품질 해석 | [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) |
| 이번 검수 근거·남은 작업 | [docs/VERIFICATION.md](docs/VERIFICATION.md) |
| 국토위성 사건·도엽 | [진행 현황](docs/폭우지역_행정동_전수목록_진행현황.md), [2024 티켓](docs/tickets/NGII-02_2024_국토위성_원본_다운로드.md), [2025 티켓](docs/tickets/NGII-01_2025_국토위성_원본_다운로드.md) |

`viirs_pipeline.py`는 공통 CMR·원본·경계 처리, `viirs_s3.py`는 기존 수집 도구입니다. `--one-per-day`는 과거 비교용이며 운영 전체 관측 수집에 사용하지 않습니다. `viirs_gold.py`는 구형 장면별 Silver용입니다. 현재 날짜별 Gold는 통합 실행기가 만듭니다. `viirs_r2_audit.py`는 읽기 전용 무결성 검사, `viirs_missing_repair.py`는 CMR 대조용입니다.

## 저장 원칙

원본은 Bronze에 보존하고 파생 결과는 원천 목록·경계 버전·처리 코드·체크섬으로 추적합니다. 미관측과 분모 0은 값을 만들어 채우지 않습니다. 구름 QC 미검증과 강수 정답 부재를 명시합니다. `.env`, 개인키, 임시 다운로드, 미리보기는 커밋하지 않습니다. 기존 추적 중인 GK2A 자료는 유지하고 신규 대용량 VIIRS·NGII는 관리대장과 재현 코드로 관리합니다.
