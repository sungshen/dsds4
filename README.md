# DSDS 관측 데이터

**2026-11-26 자료 인계:** [수집·정제·공유 안내서](docs/DATA_HANDOFF.md) · [변수 정의서](docs/VARIABLES.md) · [센서별 검사·목록](reports/2026-11-26-data-handoff/)

주 과제는 사건 전후 위성 관측과 수치 기상·환경 자료를 결합한 실제 피해 규모 추정입니다. 기존 관측·Bronze·Silver·Gold를 재사용하며 추가 대규모 수집은 하지 않습니다.

2024-01-01부터 2025-12-31까지의 위성 관측과 행정동 자료를 관리합니다. 날짜는 KST이며 명령의 `--stop`은 종료일을 포함하지 않습니다. 전체 기간의 종료 인자는 `2026-01-01`입니다.

## 데이터별 현재 구성

| 자료 | 원본·범위 | Silver | Gold·검증 상태 |
|---|---|---|---|
| VIIRS I05 | Suomi NPP Collection 2, 한국 BBOX와 교차하는 모든 영상·좌표 쌍, R2 보관(처리 완료·Hetzner 종료) | 날짜별 `all-observations.parquet`, 전국 SGIS 행정동 코드·원천 행열·관측 시각 | `dong-observations.parquet`의 행정동 × 관측 통계. 711일 게시 완료, 20일 제품쌍 미확보 별도 기록. 구름 QC 미검증 |
| 천리안 GK2A IR112 | 일별 대표 731회, 한반도 KO 2 km 격자 | 서울 425개 행정동별 raster·mask, 관측은 TIFF의 731개 band | 원시 DN. 온도 변환·구름 QC·강수 라벨 미완료 |
| 국토위성 NGII ARD | 2024 E1·E3, 2025 E4·E6 원본 1,860건의 이력·메타데이터 | 관리대장만으로 자체 Silver 검증 완료를 뜻하지 않음 | 원본은 현재 checkout에 없음. 메타데이터·사건 연결·자체 QC 제약은 인계서에 명시 |

VIIRS 파일 하나에는 여러 행정동·관측이 들어갑니다. GK2A는 행정동별 TIFF 방식입니다. VIIRS 375 m급 제품과 GK2A IR112 2 km 격자는 고해상도 지상 사진이 아닙니다.

## 플랫폼 실행

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r platform/requirements.txt
cp .env.example .env   # 기존 .env가 있으면 덮어쓰지 마세요
# .env에 R2 읽기 설정 입력. Hetzner SSH는 현재 필요하지 않음
PYTHONPATH=platform .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

브라우저에서 <http://127.0.0.1:8765>를 엽니다. Gold는 R2의 실제 산출물을 읽습니다. PostgreSQL 설치와 SQL 연결은 필요하지 않습니다. 과거 로컬 SQLite의 7,108행은 전체 Gold 건수가 아닙니다.

## 코드와 문서

| 목적 | 위치 |
|---|---|
| VIIRS 운영 수집·재개 | [VIIRS.md](VIIRS.md), [통합 실행기](viirs-full-rebuild-hetzner/viirs_full_rebuild.py) |
| 플랫폼 사용·설정 | [platform/README.md](platform/README.md) |
| 완료 기록·서버 재현 가이드 | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Silver·Gold 단위와 품질 해석 | [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) |
| 11월 23일 검수 근거·후속 인계 연결 | [docs/VERIFICATION.md](docs/VERIFICATION.md) |
| 국토위성 사건·도엽 | [진행 현황](docs/폭우지역_행정동_전수목록_진행현황.md), [2024 티켓](docs/tickets/NGII-02_2024_국토위성_원본_다운로드.md), [2025 티켓](docs/tickets/NGII-01_2025_국토위성_원본_다운로드.md) |

`viirs_pipeline.py`는 공통 CMR·원본·경계 처리, `viirs_s3.py`는 기존 수집 도구입니다. `--one-per-day`는 과거 비교용이며 운영 전체 관측 수집에 사용하지 않습니다. `viirs_gold.py`는 구형 장면별 Silver용입니다. 현재 날짜별 Gold는 통합 실행기가 만듭니다. `viirs_r2_audit.py`는 읽기 전용 무결성 검사, `viirs_missing_repair.py`는 CMR 대조용입니다.

## 저장 원칙

교수·조교·다른 팀은 인계서의 공개 목록·변수·처리 코드를 확인하고, 별도 전달되는 7일 링크로 R2 파일을 받을 수 있습니다. 서명 링크·환경설정·개인키는 Git에 올리지 않습니다.

별도 실험인 [천리안 다음 1시간 폭우 CNN](sideprojects/gk2a_rain_cnn/README.md)은 직접 수집한 천리안 영상과 서울 108 관측소 강수로 구성합니다. 초기 실행은 사건을 선택한 작은 시험이며 예측 성능을 입증한 모델과 구분합니다.

원본은 Bronze에 보존하고 파생 결과는 원천 목록·경계 버전·처리 코드·체크섬으로 추적합니다. 미관측과 분모 0은 값을 만들어 채우지 않습니다. 각 산출물의 구름 QC와 강수 정답 결합 상태를 명시합니다. `.env`, 개인키, 임시 다운로드, 미리보기는 커밋하지 않습니다. 기존 추적 중인 GK2A 자료는 유지하고 신규 대용량 VIIRS·NGII는 관리대장과 재현 코드로 관리합니다.
