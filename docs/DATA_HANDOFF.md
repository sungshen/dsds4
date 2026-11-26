# 폭우 위성 데이터 인계서

기준일: **2026-11-26 (KST)**. 기간: **2024-01-01~2025-12-31**. 기존 관측과 Bronze·Silver·Gold를 유지하면서 누락된 목록·변수 설명·검사·공유 자료를 보완했다. 추가 대규모 수집, crop 재생성, Gold 재집계를 수행하지 않았다.

주 과제는 수치 기상·환경 자료와 사건 전후 위성 관측을 결합한 **실제 피해 규모 추정**이다. 별도 CNN 실험의 다음 1시간 강수 라벨은 피해 규모 정답으로 사용하지 않는다. 수치1팀과 위성4팀의 공통 사건·지역·피해 자료를 합의하기 전까지 저장소의 사건 ID와 위성 통계를 독립된 관측 자료로 제공한다.

## 먼저 볼 자료

| 자료 | 내용 |
|---|---|
| [전체 데이터 목록](../reports/2026-11-26-data-handoff/dataset_catalog.csv) | 출처·기간·저장 위치·단위·행 단위·현재 접근 상태 |
| [통합 검사 요약](../reports/2026-11-26-data-handoff/summary.json) | 센서별 수량과 검사 범위, 해결되지 않은 준비 상태 |
| [변수 정의서](VARIABLES.md) | 원천/정제 변수, 단위, null, QA 분모, 식별자 |
| [VIIRS 영상·파일·날짜 목록](../reports/2026-11-26-data-handoff/viirs/README.md) | 장면별 센서·시각·계보, 9,796개 객체, 20개 결측 날짜 |
| [VIIRS Gold 실제 내용 검사](../reports/2026-11-26-data-handoff/gold/README.md) | 711개 전체 바이트 SHA-256, 중복 키·결측·집계·온도 수치 규칙 |
| [GK2A 원본·정제·raster 목록](../reports/2026-11-26-data-handoff/gk2a/README.md) | 736개 원본, 731개 대표 관측, 서울 425개 동의 기존 TIFF·mask 검사 |
| [NGII 영상·band·mask 목록](../reports/2026-11-26-data-handoff/ngii/README.md) | 1,860개 제품과 1,862개 사건 연결, 7,440개 band·13,020개 제공 mask 메타데이터 |
| [사건 대장](../tables/ngii/event_registry.csv) | E1–E7 날짜·대상 지역·공간 기준. 협업 합의 상태 포함 |

## 수집 원본과 현재 보관

| 자료 | 출처·센서 | 보관·원천 단위 | 정제 결과·현재 접근 |
|---|---|---|---|
| VIIRS | NASA LAADS/CMR, Suomi-NPP VIIRS, VNP02IMG·VNP03IMG Collection 2 | R2 `viirs-daily/bronze/YYYY/MM/DD/<원본명>.nc`; 영상·좌표 3,476쌍(6,952개) | R2 날짜별 Silver·Quality·Gold·완료 표식. 711일 게시, 20일 `NO_PAIRED_CMR` |
| GK2A IR112 | 기상청 API허브, GK-2A AMI LE1B IR112 KO | 저장소 `data/bronze/gk2a/IR112_KO/`; 736개 NetCDF, 751,067,060 bytes | `data/silver/gk2a/IR112_KO/`; 서울 425개 동마다 731-band TIFF와 mask. GitHub에서 기존 파일 제공 |
| NGII ARD | 국토지리정보원 국토정보플랫폼, CAS500-1 AEISS-C Level3A | `tables/ngii/asset_manifest.csv`에 1,860개 zip의 출처·경로·SHA-256 보존 | 다운로드 당시 다른 환경의 완료 기록과 XML은 있음. **현재 checkout의 원본 zip은 0개**. 자체 Silver·픽셀 QC는 미실시 |

VIIRS 전체 R2 객체는 약 **1.21 TB(십진 단위)**이며 그중 원본은 약 1.20 TB, Silver 약 12.78 GB, Gold 약 224 MB다. 원본 전부를 다시 내려받지 않고 목록·계보·기존 처리 기록으로 관리한다. 객체별 정확한 바이트 수는 VIIRS `objects.csv`에 있다.

NGII 목록의 `stored_path`는 다운로드 당시의 저장 규칙이다. 현재 Mac·GitHub·R2에 실제 파일이 있다는 보증이 아니다. 교수·조교·다른 팀에 제공할 때 이 상태를 함께 전달한다.

## 정제·무결성 점검

| 항목 | 이번에 확인한 내용 | 해석 범위 |
|---|---|---|
| 목록·중복 | VIIRS 날짜별 최종 JSON으로 누적 실행 로그의 중복 날짜 제외. GK2A 원본 경로·제품·시각·바이트 중복 검사. NGII 대장·XML 1:1 대조 및 공유 영상 2건을 사건 연결표로 표현 | 실제 다른 관측을 중복으로 지우지 않음. 원본 영상 복사 없음 |
| 결측 | VIIRS 20일을 사유와 함께 보존. GK2A 5개 추가 원본은 Silver 미선택으로 명시. NGII 미보유 원본·사건 후 영상 미확보 상태 기록 | 미관측·분모 0·미검증을 0으로 채우지 않음 |
| 형식·단위 | 센서·제품·촬영 시각·CRS·band·원천 sampling 확인. VIIRS는 K, GK2A는 packed DN. NGII는 XML의 제공 값·단위 유지 | 제공기관 ARD의 처리 수준을 자체 QC 완료로 바꾸지 않음 |
| 실제 값·이상 수치 | GK2A Silver/mask 전 band와 기존 QC 310,675행 대조. VIIRS Gold 전체의 양수·유한 K, 최소≤평균≤최대, 유효 비율·null 규칙과 장면/일별 합계 검사 | 온도 범위 진단이며 과학적 편향·기후 이상치 판정은 별도. 임의 범위 삭제·보간 없음 |
| 무결성·계보 | GK2A 736개 원본+850개 TIFF/mask 전체 SHA-256. VIIRS 전체 작은 JSON SHA·게시 계보, Gold 전체 바이트 SHA. NGII 다운로드 당시 SHA·현재 대장/보존 XML 검사 | 이번에 VIIRS 대용량 Bronze·Silver 전체 바이트를 다시 읽지는 않음. NGII 원본 바이트 재검증 불가 |

현재 구름·그림자 QC는 `unverified`다. VIIRS QA=0과 GK2A mask=1은 각각 통계 포함·DN 유효성 규칙이며 구름 없음의 증명이 아니다. 온도·DN·마스크·구름 비율·피해 규모를 서로 대체하지 않는다.

## 시간·공간 기준

- 날짜 범위는 KST `[2024-01-01, 2026-01-01)`이며 원천 UTC와 실제 관측 시각도 보존한다. 명령의 `--stop`은 종료일 제외다.
- VIIRS는 각 granule 시작·끝 UTC와 TAI93 스캔 시작을 구분한다. 하루 여러 관측은 별도 `swath_id`로 유지한다. 기존 영상 3,476개의 제공기관 관측 footprint는 `viirs/scene_footprints.csv`에 따로 보존한다. CMR 검색 BBOX `[124,32,132,39]`는 검색 조건이다. 제공 footprint·좌표 envelope도 행정동 안 실제 픽셀 coverage와 구분한다.
- GK2A는 원래 수집된 일별 대표 표본이다. 727개는 09:00 KST, 나머지 4개는 실제 08:58/11:00 시각을 유지한다. 2분 전수 자료로 표시하지 않는다.
- NGII는 XML의 촬영일과 처리일을 구분한다. 첫 입력 영상의 UTC 중심 시각·7자리 소수초를 보존하되 정확한 도엽 노출 시각으로 확장하지 않는다.
- VIIRS 행정동은 연도별 SGIS 2Q(2024 3,558개, 2025 3,559개)이다. GK2A는 10자리 `adm_cd2`의 경계 파일 해시로 버전을 고정하며 유효 기간은 미확인이다. NGII 검색 동 목록은 2021-04 vuski 경계다. **공통 경계와 코드 변환표가 준비되기 전에는 코드 문자열을 직접 조인하지 않는다.**
- VIIRS는 원천 swath 픽셀 중심, GK2A는 원천 2 km LCC 격자, NGII는 제공 EPSG:5179·2 m product sampling을 유지한다. 센서 간 비교용 재격자화는 수행하지 않았다.

## 코드·처리 규칙과 재현

원래 처리 코드는 [데이터 계약](DATA_CONTRACT.md), [VIIRS 가이드](../VIIRS.md), `crop_gk2a_silver.py`, `viirs-full-rebuild-hetzner/viirs_full_rebuild.py`에 보존했다. 각 inventory에 입력 목록·경계·코드·파일 해시를 기록한다. Hetzner는 처리가 끝나고 2026-11-26 삭제했으므로 재현에 상시 서버를 요구하지 않는다.

아래 명령은 관측을 재수집하거나 raster를 다시 생성하지 않는 **목록·검사**다. 이미 전달된 동일 버전의 산출물을 사용할 때는 다시 실행할 필요가 없다.

```bash
# 기존 로컬 원본·Silver 점검
.venv/bin/python scripts/gk2a_delivery_inventory.py
.venv/bin/python scripts/ngii_delivery_inventory.py

# 기존 R2 목록·작은 품질 JSON 점검; 원본/Silver 다운로드 없음
.venv/bin/python scripts/viirs_delivery_inventory.py

# 필요할 때 기존 장면의 공개 공간 메타데이터만 재현(새 영상 수집 없음)
.venv/bin/python scripts/viirs_scene_footprints.py --offline

# 기존 Gold만 읽는 검사. 정확히 같은 소스/검사 기준의 PASS는 재사용
.venv/bin/python scripts/verify_handoff_gold.py --reuse

# 통합 요약·카탈로그·체크섬 갱신
.venv/bin/python scripts/build_data_handoff.py
```

VIIRS 최초 목록 정리의 서버 종료 기록은 비공개 로컬 백업에서 날짜별 JSON만 추출했다. 공유 폴더에는 `.env`·개인키·서버 비밀번호를 넣지 않고 백업 SHA-256과 안전한 검사 결과만 남겼다. 새 환경은 Git의 인계 목록·보고서를 그대로 사용하거나 보존된 작은 JSON 캐시로 `--offline` 재현할 수 있다. 실제 인증키는 저장소 밖에서 준비한다.

## 교수·조교·다른 팀의 접근

1. 공개 저장소 [sungshen/dsds4](https://github.com/sungshen/dsds4)의 이 문서에서 inventory·변수·검사 결과·전처리 코드를 읽는다. GK2A 파일은 이미 추적 중인 `data/` 경로를 그대로 사용한다.
2. VIIRS 실제 원본·Silver·Gold는 **7일 만료 GET 다운로드 안내 링크**를 별도로 전달한다. 수신자는 API 키·서버 로그인 없이 날짜·파일 종류를 선택해 받는다. 제공 링크는 읽기 전용이며 R2 버킷의 공개 설정을 바꾸지 않는다.
3. 공유 화면의 ZIP에는 목록·변수 설명·기존 QC·처리 코드와 파일 체크섬이 들어 있다. 영상 전부가 ZIP에 들어 있는 것은 아니다. 영상 파일은 개별 다운로드 목록과 연결된다.
4. NGII 원본은 현재 이 환경에서 접근하지 못한다. 원본 보관 환경의 경로·권한을 확보하기 전에는 픽셀 분석용 다운로드가 제공된 것으로 표시하지 않는다.

공유 링크·서명 CSV·HTML·ZIP은 Git 제외인 `data/platform/sharing/`에서 관리한다. 링크는 수업 관계자에게만 전달하며 만료 후 필요할 때 재생성한다. 원본·파생 관측은 복사하지 않고 `handoff/`에 작은 안내 패키지만 별도 저장한다.

```bash
# 7일 링크 준비: 기본 실행은 로컬 공유 묶음만 작성
.venv/bin/python scripts/create_handoff_links.py
# 안내 묶음만 비공개 R2에 게시하고 수신자용 링크 생성
.venv/bin/python scripts/create_handoff_links.py --publish
```

## 남은 활용 제약과 협업 인계

| 항목 | 현재 상태·다음 연결 조건 |
|---|---|
| 실제 피해 규모 정답 | 공식 피해 데이터·출처·단위·확정일·사건/지역 연결표 미확보. 수치1팀과 협업 필요 |
| 공통 사건 목록 | NGII E1–E7은 저장소 내부 목록. 협업팀의 사건 ID·시작/끝 시각·대상 지역 합의 필요 |
| 행정경계 | SGIS/vuski/10자리 동 코드 기준이 달라 공통 경계 또는 버전별 변환표 필요 |
| VIIRS 20일 | 검색 조건에 맞는 제품쌍 없음. 값을 만들지 않으며 NASA 전체 무관측으로 단정하지 않음 |
| GK2A 시간 해상도 | 일별 대표 영상. 시간 단위 폭우 전개를 복원하거나 2분 전수 시계열로 사용할 수 없음 |
| 구름·그림자·정량 온도 QC | 미완료 상태 명시. 불확실한 관측을 구름 없는 피해 관측으로 해석하지 않음 |
| NGII | 원본 현재 접근 불가·자체 Silver/QC 미실시. 사건 당일 영상 없음, 같은 지역 전후 영상도 제한적 |
| 강수·Sentinel 보조 | 주 자료와의 공통 사건 결합 미실시. Sentinel SCL·지수 등 미확보 변수는 생성하지 않음 |
| 과거 오류 로그 | 현 파일의 인증키 3건은 가림 처리. 과거 Git 이력은 유지되므로 노출된 KMA 키 재발급 필요 |

이 제약은 [통합 요약](../reports/2026-11-26-data-handoff/summary.json)과 센서별 검사 결과에도 함께 기록한다. 자료 인계 문서가 있다는 이유로 피해 모델용 입력·정답 결합이나 과학적 품질 검증을 완료로 표시하지 않는다.
