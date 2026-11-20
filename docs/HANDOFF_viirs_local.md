# VIIRS 작업 인수인계 (클라우드 세션 → 로컬 Claude Code)

> 작성: 2026-11-19 · 진행자/작성자: 정유경
> 이 문서만 읽고 로컬(맥북)에서 VIIRS 작업을 이어가도록 정리했다.
> 과제 전체 맥락은 기존 인수인계 문서(`DSDS_HANDOFF.md`, 가이드라인 전문 포함)를 함께 본다. 가이드라인과 이 문서가 충돌하면 **가이드라인이 우선**이다.

---

## 0. TL;DR

1. **2024-07 ~ 2024-12 Bronze → Silver 완료** (클라우드). Bronze 체크 12/12, Silver 체크 16/16 PASS.
2. 브랜치 `viirs-bronze-2024-07`을 **패치로 적용 → 푸시**한다 (§2).
3. 원본(.h5)과 Silver 이미지는 클라우드에만 있었고 세션과 함께 사라진다. 보관이 필요하면 로컬에서 **재다운로드 → Silver 재생성**한다 (§3, §5). 원본 약 41 GB가 필요하다.
4. §7의 미해결 질문을 확인한다. 특히 행정동 경계 버전, VIIRS 사용 합의, 그림자·저태양 규칙.

---

## 1. 지금까지 한 일 (요약)

| 단계 | 상태 | 내용 |
|---|---|---|
| 검색 | ✅ 2024-01 ~ 2025-12 | VNP09GA(Suomi-NPP) / VJ109GA(NOAA-20) / VJ209GA(NOAA-21), v002, 타일 h28v05, `search_results` 2,238행 |
| Bronze | ✅ 2024-07 ~ 12 | 536 granule(.h5 + .cmr.xml, 약 41 GB), 실패 0, sha256, `observation_calendar`, 체크 12/12 |
| Silver | ✅ 2024-07 ~ 12 | 서울 crop, 면적가중치, 마스크 v2, 지수, 행정동 이미지 455,600개, `silver_index` 227,800행, 체크 16/16 |
| Gold / Baseline | ⏳ 미착수 | |

**월별 결과** (서울 평균, 상세는 `docs/viirs_silver.md`)

| 월 | 구름 | 유효 | 위성 1개 이상 usable 인 날 | 행정동별 유효일 중앙값 |
|---|---|---|---|---|
| 07 | 0.85 | 0.03 | 13 | 2 (0일인 동 21개) |
| 08 | 0.50 | 0.18 | 25 | 8 |
| 09 | 0.47 | 0.29 | 24 | 12 |
| 10 | 0.50 | 0.36 | 19 | 14 |
| 11 | 0.40 | 0.46 | 23 | 18 |
| 12 | 0.27 | 0.57 | 25 | 22 |

- 결측 (2024 후반부): SNPP 18일(07 12일, 09 2일, 11 4일), NOAA-21 2024-11-12 서울 관측 없음(`no_coverage_over_aoi`).
- 클라우드 디스크 한도 때문에 2024-07·08·09 `.h5` 262개는 Silver 처리 후 삭제했다. 기록은 `local_retained=False`이고 sha256과 `.cmr.xml`은 보존했다. 10~12월 274개는 클라우드에 남아 있지만, 세션이 끝나면 함께 사라진다.

상세 문서
- `docs/viirs_bronze.md`, `docs/viirs_silver.md`: 규칙, 결과, 한계
- `docs/figures/silver_check_2024H2_monthly.png`: 월별 위치 확인 그림
- `tables/band_codebook.csv`, `tables/variable_definitions.csv`: codebook, 변수 정의서

---

## 2. Git 상태와 적용 방법

### 2.1 상태 (2026-11-19 기준)
- `origin/master` 최신은 `4bd700c9 Generate GK2A IR112 Silver crops for Seoul` (성신님)
- 우리 브랜치 `viirs-bronze-2024-07` = origin/master + 아래 8개 커밋 (**최신 master 위로 rebase 완료**)

| 커밋 | 내용 |
|---|---|
| `Add VIIRS L2G Bronze pipeline and 2024-07 Bronze tables` | 검색·다운로드·calendar·체크 스크립트, Bronze 표 |
| `Add VIIRS Silver (2024-07): admin-dong crop, QA masks, indices` | Silver 스크립트, `silver_index.csv.gz`, codebook, 정의서, 위치 확인 그림 |
| `Add per-dong Silver images/masks, guideline column names, usable_count` | 행정동 이미지·마스크, 가이드 컬럼명, `usable_count` |
| `Prepare VIIRS download for local runs (...)` | `.env` 토큰 읽기, `--months` 옵션, 대상 월 2024-07 ~ 12, `requirements-viirs-l2g.txt` |
| `Ignore data/viirs (...)` | **master에서 `data/`가 ignore 목록에서 빠져서** `data/viirs/`만 따로 ignore |
| `Support month-by-month VIIRS runs under a disk limit` | `--months` 월 단위 처리, 원본 로컬 삭제 스크립트, 체크 보완 |
| `VIIRS Bronze/Silver for 2024-07..2024-12 (536 granules)` | 6개월 결과 표, 월별 위치 그림, 문서 갱신 |
| `Add VIIRS local handoff document` | 이 문서 |

- 클라우드 세션에서는 Claude GitHub App이 저장소에 없어서 푸시가 403으로 거부됐다. 그래서 **패치 파일 `viirs-local.patch`** 로 전달한다.

### 2.2 맥북에서 적용

```bash
cd ~/study_DSDS/dsds4          # 없으면: git clone https://github.com/sungshen/dsds4 ~/study_DSDS/dsds4
git checkout master
git pull origin master          # 88395b0f 이상이어야 함
git checkout -b viirs-bronze-2024-07     # 이미 있으면: git branch -D viirs-bronze-2024-07 후 다시
git am ~/Downloads/viirs-local.patch
git log --oneline -9            # VIIRS 커밋 8개 확인
git push -u origin viirs-bronze-2024-07  # 본인 계정에 쓰기 권한 필요
```

`git am`이 실패하면 `git am --abort` 후 원인을 확인한다. master가 위 커밋 이후 더 바뀌었으면 충돌이 날 수 있다.

---

## 3. 로컬 환경 준비

```bash
cd ~/study_DSDS/dsds4
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-viirs-l2g.txt
```

- `.env` (커밋 금지, `.gitignore`에 있음):
  ```
  EARTHDATA_TOKEN=<urs.earthdata.nasa.gov → Generate Token 에서 발급, 60일 유효>
  ```
- 토큰은 코드, 로그, 노션, 커밋에 넣지 않는다. `viirs_bronze_check.py`가 표·코드·설정의 토큰 노출을 검사한다.
- 디스크: 2024-07 ~ 12 원본 **약 41 GB**, Silver 행정동 이미지는 파일 수가 많다(아래 §5.3). 외장 디스크를 쓰려면 `data/viirs`를 심볼릭 링크로 연결한다.
  ```bash
  mkdir -p /Volumes/<외장>/dsds4_viirs && ln -s /Volumes/<외장>/dsds4_viirs data/viirs
  ```

---

## 4. 파이프라인 (스크립트와 순서)

| 순서 | 명령 | 하는 일 | 네트워크 |
|---|---|---|---|
| 1 | `python scripts/viirs_search.py` | CMR 전수 검색(config 기간) → `tables/search_results.csv`, `search_log.csv`, `data/viirs/bronze/search_raw/` | CMR (인증 불필요) |
| 2 | `python scripts/viirs_download.py [--months 2024-08 ...]` | 원본 .h5 + .cmr.xml → `data/viirs/<제품>/<YYYY-MM>/bronze/originals/<product_id>/`, `asset_manifest.csv`, `download_log.csv` | LP DAAC (토큰) |
| 3 | `python scripts/viirs_calendar.py` | `observation_calendar.csv` (결측일 원인은 L1B를 CMR로 조회) | CMR |
| 4 | `python scripts/viirs_bronze_check.py` | Bronze 체크 → `tables/bronze_check.txt`, `granule_metadata.csv` | 없음 |
| 5 | `python scripts/viirs_silver.py` | manifest의 **모든** granule → 서울 crop, 마스크, 지수, `silver_index.csv.gz`, `silver_observations.csv`, `overpass_candidates.csv` | CMR (통과시각) |
| 6 | `python scripts/viirs_silver_dong.py` | 행정동 이미지·마스크, 가이드 컬럼, `usable_count` | 없음 |
| 7 | `python scripts/viirs_silver_check.py` | Silver 체크 16개 → `tables/silver_check.txt` | 없음 |

**주의**
- 1번은 이미 완료된 결과가 커밋되어 있다(2024–2025). 다시 돌리면 `search_results`가 덮어써지고 `download_status`가 초기화된다. **보통은 건너뛴다.**
- 2번은 재실행해도 안전하다. 받은 파일은 건너뛰고 manifest를 갱신한다.
- 5번을 돌리기 전에 기존 Silver 폴더(`data/viirs/*/*_viirs_silver_v2`)가 있으면 덮어쓴다. 처리 버전을 올리면 폴더명이 바뀐다.
- 6번은 오래 걸린다. 클라우드 기준 1개월(81 관측)에 약 13분, 파일 68,850개였다. 6개월이면 약 80분, 파일 약 45만 개다.
- 로컬 새 클론에는 `data/viirs/`가 없다. **2024-07 원본도 다시 받아야 한다** (2번에 2024-07 포함).

---

## 5. 로컬 재현: 2024년 후반부 (2024-07 ~ 2024-12) — 클라우드에서 완료됨, 원본·이미지를 로컬에 두려면 실행

### 5.1 규모 (`search_results` 기준, h28v05)

| 월 | NOAA-20 | NOAA-21 | SNPP | 합계 | 원본 |
|---|---|---|---|---|---|
| 07 | 31 | 31 | 19 | 81 | 6.2 GB |
| 08 | 31 | 31 | 31 | 93 | 7.0 GB |
| 09 | 30 | 30 | 28 | 88 | 6.7 GB |
| 10 | 31 | 31 | 31 | 93 | 7.3 GB |
| 11 | 30 | 30 | 26 | 86 | 6.6 GB |
| 12 | 31 | 33 | 31 | 95 | 7.7 GB |
| 계 | 184 | 186 | 166 | **536** | **40.5 GB** |

NOAA-21 12월 33개는 2024-12-11의 재처리본 3개 때문이다(모두 보존).

### 5.2 실행 순서

```bash
source .venv/bin/activate
python scripts/viirs_download.py --months 2024-07      # 먼저 한 달로 동작 확인
python scripts/viirs_download.py                        # config 의 07~12 전체 (재실행 안전)
python scripts/viirs_calendar.py
python scripts/viirs_bronze_check.py                    # 11/11 PASS 확인
python scripts/viirs_silver.py
python scripts/viirs_silver_dong.py
python scripts/viirs_silver_check.py                    # 16/16 PASS 확인
```

- 재다운로드한 원본의 sha256이 manifest 값과 같으면, 클라우드에서 만든 결과와 같은 원본임이 확인된다. 다만 `viirs_download.py`는 받은 파일로 manifest를 갱신하므로, 비교하려면 실행 전에 `tables/asset_manifest.csv`를 복사해 둔다.
- 디스크가 부족하면 월 단위로 실행한다: `--months 2024-08` → `viirs_silver.py --months` → `viirs_silver_dong.py --months` → 필요하면 `viirs_release_originals.py --months`.

### 5.3 확인할 점
- 체크 실패가 있으면 멈추고 원인을 기록한다.
- 월별 요약: 관측 수, 서울 구름·그림자·유효 비율, `usable_count`, 행정동별 유효일 수를 `tables/silver_observations.csv`, `observation_calendar.csv`에서 뽑는다.
- **10월(맑은 달)** 로 Silver 품질 검증을 한다. 그림자 플래그가 과다한지, 지수 값의 범위가 상식적인지 본다.
- **11~12월**: 눈 마스크(QF2 b5), 저태양(QF1 b5) 비율을 확인한다. 저태양은 현재 마스크에 없다. 비율이 높으면 정책을 결정해야 한다.
- 위치 확인 그림을 월마다 1장(가장 맑은 날)씩 만든다. `docs/figures/` 방식을 참고한다.
- `silver_index.csv.gz`는 약 6배(약 17 MB)로 커지므로 GitHub 업로드에 문제는 없다. 행정동 이미지는 `data/viirs/` 안이라 git에 올라가지 않는다.
- 끝나면 `docs/viirs_silver.md`와 진행상황 md를 6개월 기준으로 갱신하고 커밋한다.

### 5.4 (선택) 용량을 줄이는 대안
NASA OPeNDAP 부분 추출(필요 밴드 + 서울/한국 범위)을 쓰면 수십 배 줄어든다. 다만 원본 파일이 아니라서 Bronze 원칙과 차이가 생긴다. 쓰려면 원본과 값이 동일한지 대조 검증하고, 조교 확인을 먼저 받는다.

---

## 6. 핵심 설계 결정 (바꾸기 전에 이유 확인)

| 결정 | 이유 |
|---|---|
| 원천 격자 유지 (sinusoidal, 재투영·resize 없음) | 가이드: 원천 해상도 보존. 500 m 격자 463.313 m, 1 km 격자 926.625 m |
| 행정동 × 픽셀 **면적 가중치** (`admin_pixel_weights_*.csv`) | 동 면적 중앙값 0.99 km². 픽셀 중심 방식이면 1 km에서 70개 동이 0픽셀. 가중치 합 = 동 면적(오차 1e-12) |
| 행정동 이미지 = 동을 감싸는 최소 창 + 내부비율 마스크(0~1) | 가이드 형식(동별 tif + 동 밖 mask)과 면적 정확도를 둘 다 충족 |
| QA 비트는 원천 HDF5의 `QA index` 속성 원문 기준 | 문서 기억 대신 파일 내 정의 사용. 원문은 `crop_index.json`에 보존 |
| 마스크 v2: AOT/SR 전체품질 bad는 기록만 | QF5/QF6 밴드별 bad = QF4 b4(AOT bad)와 100% 동일. 맑은 육지의 40~72%에서 켜짐. v1(제외)은 유효 ≈ 0이었다. strict 값은 별도 컬럼 |
| 그림자(QF2 b3)는 제외 | 가이드 원칙. 구름이 일부 낀 날 구름 주변에 집중(월 평균 12~31%), 맑은 날에는 거의 없음. **팀 결정 필요** |
| 저태양(QF1 b5)은 마스크 미포함 | 12월 1 km 픽셀의 11.5%. **정책 결정 필요** |
| 원본 로컬 삭제(`viirs_release_originals.py`) | 클라우드 디스크 한도. Silver 처리 완료 후 디스크가 부족할 때만, 가장 오래된 월부터 삭제. 기록 보존 |
| 음수 반사도 픽셀은 지수 null | 원천 valid_range가 -0.01까지 허용(수면). 그대로 두면 지수가 1을 넘는다 |
| 500 m QA는 1 km QF를 2×2 nearest로 대응, `iobs_res_1 != 0` 제외 | 500 m 그리드에 자체 QF가 없다 |
| QF6 M10 비트 = bit 1로 가정 | 원천 문서에 bit 2가 M11·M10으로 중복 표기. standard 결과에는 영향 없음 |
| 대표값 `coverage_fraction` 등 = 500 m 값 | 가이드 컬럼명 충족. 격자별 값은 `_500m`, `_1km`로 유지 |
| `usable_count` = 그날 granule 중 valid_fraction_500m ≥ 0.5인 동이 1개 이상인 수 | 규칙은 `usable_rule` 컬럼에 기록 |
| 촬영시각 = 날짜 + `overpass_window_*` (그날 서울 상공 주간 L1B granule 시각 범위) | L2G 일합성이라 픽셀별 시각이 없다 |

---

## 7. 미해결 질문 (확인 필요)

1. **행정동 경계 출처·버전**: VIIRS는 `admin_boundary_version = unknown`이다. 성신님의 GK2A Silver는 geojson의 **sha256**을 버전으로 썼다(`sha256:9102705186…`). → VIIRS도 같은 방식으로 맞추고, 실제 출처·기준일을 확인한다.
2. **VIIRS 사용·기간 합의**: 가이드에 없는 원천이다(추가 보조 원천). 2024 후반부만 진행하는 범위도 팀·조교와 맞춘다.
3. **전국 vs 서울**: 지금은 서울 425개 동만 처리했다. 전국이면 타일 h27v05가 추가로 필요하고(경기·강원 북서부), 전국 경계 파일이 필요하며, 용량이 약 2배가 된다.
4. **그림자·저태양 규칙**: 그림자 유지 여부, 12월 저태양 플래그(11.5%)를 제외할지
5. **수치팀 합의**: 조인 키(`adm_cd2` 10자리 vs 7자리), 시간대·라운딩, `max_image_age_hours`, Label 형식
6. **Baseline**: VIIRS 행정동 이미지는 4×7 픽셀 수준이다. 조교에게 ResNet-18 범위를 확인한다.

---

## 8. GitHub 리뷰: 팀 전체에서 더 해야 할 일

### 8.1 master 현황 (4bd700c9)
- **GK2A 재수집 완료 (성신님)**:
  - `data/bronze/gk2a/IR112_KO/` .nc 727개(약 1 MB, 정상 NetCDF)
  - 2024-01-01 ~ 2025-12-31, **하루 1장(00:00 UTC = 09 KST)**
  - manifest의 status는 success 726, already_exists 1
- **GK2A Silver**: `crop_gk2a_silver.py`, 서울 425개 동 × 일별 `admin_crop.tif`·`quality_mask.tif`
  - 2 km LCC 원천 격자, `all_touched` 마스크
  - DN 무보정, `cloud_qc_status = unverified`
  - 위치 참조 검증(최대 오차 7.9e-6°)과 처리 해시 기록이 잘 되어 있다.
- 예전 오류 파일(58바이트 .nc 17,541개)은 삭제됐다.

### 8.2 더 진행해야 할 것

| 우선 | 항목 | 내용 |
|---|---|---|
| 🔴 | **1시간 단위 요구** | GK2A가 하루 1장(00 UTC)만 있다. 과제 목표가 "1시간 × 행정동"이므로 GK2A는 최소 1시간 간격(가능하면 10분)으로 늘려야 한다. 일일 호출 한도도 확인한다 |
| 🔴 | **Gold / 수치팀 결합** | 아무 원천도 Gold가 없다. `forecast_reference_time`별 최근 관측 + `image_age_hours` + `image_match_status` 결합표, 수치팀과 키·시간대 합의 |
| 🔴 | **주 원천(국토위성)** | 가이드의 주 경로가 아직 0%다. 이용신청 상태 확인 → 1세트 → `register_crop.py` |
| 🔴 | **행정동 경계 버전 통일** | GK2A(sha256)와 VIIRS(unknown) 표기를 통일하고, 실제 출처·기준일·유효기간을 기록한다 |
| 🟡 | **원천 간 규칙 통일** | 동 경계 픽셀 처리: GK2A는 `all_touched`(경계 픽셀이 여러 동에 중복), VIIRS는 면적 가중치. 컬럼명: GK2A는 `admin_dong_code`, VIIRS는 `adm_cd2`. Gold에서 같은 키·규칙으로 맞춘다 |
| 🟡 | **GK2A Bronze 표 보완** | 가이드 필수 `search_results`, `observation_calendar`(촬영 없음 / 실패 / 품질 제외 구분)가 없다. `asset_manifest`에 `source_dataset`, `product_id`, `native_resolution_m`, `native_crs`가 없다 |
| 🟡 | **GK2A 품질** | DN → 밝기온도 변환(보정 계수) 여부, 구름 판정(L2 CLD 등) 방법 결정. 현재는 unverified |
| 🟡 | **저장소 위생** | ① `.gitignore`에서 `data/`가 빠져 GK2A 원본 약 750 MB가 git에 들어가 있다 (Git LFS나 외부 저장 검토). ② 옛 `logs/asset_manifest.csv`·`download_log.csv`가 삭제된 58바이트 파일을 가리킨 채 남아 있다 → 삭제 또는 "폐기" 표기. ③ `.DS_Store` 커밋됨. ④ `downloads/.DS_Store` 남음 |
| 🟡 | **README** | 저장소 전체 README(원천별 폴더 구조, 실행 순서, 설정 파일)가 없다. 필수 제출물이다 |
| 🟢 | **Sentinel-2 보조** | 미착수. 수역 지수(MNDWI)는 10~20 m인 Sentinel-2가 VIIRS보다 동 단위에 훨씬 적합하다 |
| 🟢 | **Baseline (ResNet-18)** | Gold 이후 |

---

## 9. 로컬 Claude Code에게 (작업 규칙)

- 사용자: 정보시스템학과 4학년, Python·pandas 익숙, 위성/GIS는 처음이다. **설명은 한국어로**, 명령은 복붙 가능하게 쓴다.
- macOS(Apple Silicon), zsh. `python` 명령이 없으면 `python3`를 쓰고, venv 활성화 후에는 `python`을 쓴다.
- 가이드 원칙:
  - 가정하지 않는다.
  - 0으로 채우지 않는다(null + 사유).
  - 촬영 없음, 수집 실패, 품질 제외를 구분한다.
  - 원본을 보존한다.
  - 토큰을 어디에도 남기지 않는다.
- `data/viirs/`는 git에 올리지 않는다(.gitignore). 표·스크립트·문서만 커밋한다.
- 큰 작업(다운로드, Silver 전체)은 시작 전에 예상 용량과 시간을 알리고 진행한다.
- 결과를 보고할 때는 체크 결과(PASS/FAIL)와 실패 사유를 그대로 전한다.
