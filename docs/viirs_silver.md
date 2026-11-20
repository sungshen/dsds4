# VIIRS Silver (2024-07 ~ 2024-12, processing_version = viirs_silver_v2)

입력: Bronze `.h5` 536개 (2024-07 ~ 12; VNP09GA 166 / VJ109GA 184 / VJ209GA 186), 서울 행정동 425개.
산출: 관측 536개 x 행정동 425개 = `tables/silver_index.csv.gz` 227,800행, 행정동 이미지 455,600개.

## 실행
```bash
python scripts/viirs_silver.py        # crop tif, crop_index.json, silver_index, silver_observations, overpass_candidates
python scripts/viirs_silver.py --months 2024-08        # 월 단위 처리(다른 월 행 유지)
python scripts/viirs_silver_dong.py --months 2024-08
python scripts/viirs_silver_check.py  # tables/silver_check.txt (16개 체크)
```

## 보완 (scripts/viirs_silver_dong.py)
- 행정동 마스크(정적): `data/viirs/silver/static/admin_mask/<adm_cd2>_{500m,1km}.tif` (동을 감싸는 최소 창, 값 = pixel 면적 중 동 내부 비율 0~1)
- 행정동 이미지(관측별): `data/viirs/<product>/<observation_id>/silver/dong/<adm_cd2>_{500m,1km}.tif` (서울 crop 에서 같은 창을 잘라낸 원천값 + mask, 68,850개)
- `silver_index` 에 가이드 컬럼명 `coverage_fraction`, `valid_fraction`, `cloud_fraction`, `shadow_fraction`(대표값 = 500m) 과
  `image_path_*`, `admin_area_mask_path_*`, `patch_height_px_*`, `patch_width_px_*` 추가.
- `observation_calendar.usable_count` = 그날 granule 중 valid_fraction_500m >= 0.5 인 행정동이 1개 이상인 granule 수.
  2024-07: usable NOAA-20 9일 / NOAA-21 8일 / SNPP 5일, 나머지는 status=quality_excluded.

## 처리 규칙
1. **격자**: 원천 sinusoidal 격자 그대로(재투영·resize·padding 없음). 서울 bbox + 2 pixel 창으로 자름.
   500m 격자(463.313 m, I1–I3 원천 375 m), 1km 격자(926.625 m, M-band 원천 750 m). 두 격자는 UL 동일, 해상도 2배로 정렬됨(코드에서 assert).
2. **행정동**: 경계를 sinusoidal(등적)로 변환해 pixel 사각형과 교차 → `data/viirs/silver/static/admin_pixel_weights_{500m,1km}.csv`
   (adm_cd2, row, col, 겹침면적/pixel면적). 가중치 합 = 행정동 면적(오차 1e-12). 픽셀 중심 방식이면 1km 에서 70개 동이 0 pixel 이 되므로 면적가중 사용.
3. **값 보존**: crop tif 에 원천 정수값(scale 1e-4, fill -28672) 그대로 저장. 반사도 변환은 지수 계산 시에만.
4. **QA**: 1km QF1–QF7 사용. 비트 정의는 원천 HDF5 의 `QA index` 속성 전문을 `crop_index.json` 에 보존.
   500m 는 1km QF 를 2x2 nearest 로 대응하고, `iobs_res_1 != 0`(500m 값이 1km QA 와 다른 관측) 은 제외.
5. **지수**: valid pixel 에서만 pixel 지수 → 행정동 면적가중 평균. 분모 ≤ 0, 음수 반사도(원천 valid_range 가 -0.01 까지 허용) pixel 은 null.
   valid 가 0 이면 null + `index_unavailable_reason`. 0 으로 채우지 않음.

## mask 비트 (mask_500m / mask_1km)
| bit | 의미 | 출처 | standard | strict |
|---|---|---|---|---|
| 1 | fill / 관측 없음 | 반사도 fill, num_observations | 제외 | 제외 |
| 2 | night | QF1 b4 | 제외 | 제외 |
| 4 | cloud (probably/confident cloudy) | QF1 b2-3 ≥ 2 | 제외 | 제외 |
| 8 | cloud shadow | QF2 b3 | 제외 | 제외 |
| 16 | snow/ice | QF2 b5 | 제외 | 제외 |
| 32 | bad SDR (지수 밴드) | QF3 (M4,M7,M10), QF4 b1-3 (I1-I3) | 제외 | 제외 |
| 64 | 500m QA 불일치 | iobs_res_1 ≠ 0 | 제외 | 제외 |
| 128 | adjacent to cloud | QF7 b1 | 기록만 | 제외 |
| 256 | thin cirrus | QF2 b6-7, QF7 b4 | 기록만 | 기록만 |
| 512 | probably clear | QF1 b2-3 = 01 | 기록만(사용) | 기록만(사용) |
| 1024 | AOT / SR overall quality bad | QF4 b4 (= QF5/QF6 밴드별 overall bad) | 기록만 | 제외 |

**v1 → v2 변경 이유**: QF5/QF6 의 밴드별 "overall quality SR bad" 가 QF4 b4 "overall quality of AOT bad" 와
100% 동일하게 켜짐을 확인(2024-07 VJ109GA, 확실히 맑은 육지 pixel 의 40–72% 에서 켜짐). 밴드 불량이 아니라 에어로졸 보정 신뢰도이므로
standard 에서 제외 조건에서 빼고 strict 와 `aot_quality_bad_fraction_*` 로 남김. v1(이를 제외) 은 서울 valid ≈ 0 이었음.

**원천 문서 모호점**: QF6 `QA index` 에 bit 2 가 M11·M10 으로 두 번 적혀 있음. M10 = bit 1 로 가정(M8 b0, M10 b1, M11 b2 순서).
v2 에서 QF6 는 strict(AOT 품질)에서만 쓰므로 standard 결과에는 영향 없음.

## 2024-07 ~ 2024-12 결과 요약 (서울 면적 기준, 월 평균)

| 월 | 관측 | 구름 | 그림자 | 유효(standard) | 유효(strict) | 위성 1개 이상 usable 인 날 | 행정동별 유효일(≥50%) 중앙값 [최소~최대] |
|---|---|---|---|---|---|---|---|
| 2024-07 | 81 | 0.85 | 0.12 | 0.03 | 0.01 | 13 | 2 [0~6], 0일인 동 21 |
| 2024-08 | 93 | 0.50 | 0.31 | 0.18 | 0.11 | 25 | 8 [4~14] |
| 2024-09 | 88 | 0.47 | 0.22 | 0.29 | 0.21 | 24 | 12 [8~16] |
| 2024-10 | 93 | 0.50 | 0.13 | 0.36 | 0.26 | 19 | 14 [11~18] |
| 2024-11 | 86 | 0.40 | 0.12 | 0.46 | 0.25 | 23 | 18 [14~21] |
| 2024-12 | 95 | 0.27 | 0.13 | 0.57 | 0.06 | 25 | 22 [17~25] |

- `usable_count` 일수(6개월): NOAA-20 114/184, NOAA-21 113/184, SNPP 108/166.
- 지수 값이 있는 행: 약 39% (NDVI, MNDWI). NDVI 월 중앙값 0.34(7월) → 0.16(12월), 계절 변화와 일치.
- **NOAA-21 2024-11-12**: granule 은 있으나 서울 픽셀이 모두 fill, 그날 서울 상공 주간 L1B 0건 → `observation_calendar.status = no_coverage_over_aoi`.
- 품질 진단 (covered 픽셀 중 비율):

| 월 | 눈(QF2 b5) | 그림자 | AOT 품질 bad | 맑은 픽셀 중 AOT bad | 저태양(QF1 b5, 1 km) |
|---|---|---|---|---|---|
| 07 | 0.000 | 0.12 | 0.99 | 0.93 | 0.000 |
| 08 | 0.000 | 0.31 | 0.88 | 0.76 | 0.000 |
| 09 | 0.000 | 0.22 | 0.78 | 0.58 | 0.000 |
| 10 | 0.001 | 0.13 | 0.72 | 0.45 | 0.000 |
| 11 | 0.013 | 0.12 | 0.73 | 0.56 | 0.022 |
| 12 | 0.001 | 0.13 | 0.94 | 0.92 | 0.115 |

  - AOT bad 플래그는 맑은 픽셀에서도 45~93% → standard 에서 기록만 하는 v2 판단을 6개월 결과가 뒷받침. 다만 12월 strict 유효는 0.06 으로 급감.
  - 12월 저태양 플래그 11.5% (현재 마스크에 미포함). 정책 결정 필요.
  - 눈 플래그는 11월 말 대설에도 1.3% 수준(대설 당일은 구름으로 가려짐).
  - 그림자는 구름이 일부 낀 날 구름 주변에 집중, 맑은 날에는 거의 없음 (`docs/figures/silver_check_2024H2_monthly.png`).
- 위치 확인: 월별 가장 맑은 관측에서 한강 저반사 띠가 강변 행정동 경계를 따라감 (`docs/figures/silver_check_2024H2_monthly.png`).

## 2024-07 결과 요약 (최초 1개월) (서울 면적 기준 평균)
| 위성 | cloud (covered 중) | shadow (covered 중) | valid standard | valid strict |
|---|---|---|---|---|
| NOAA-20 VJ109GA | 0.83 | 0.12 | 0.05 | 0.013 |
| NOAA-21 VJ209GA | 0.84 | 0.12 | 0.03 | 0.013 |
| SNPP VNP09GA | 0.88 | 0.11 | 0.01 | 0.002 |

- 행정동별 "어느 위성이든 valid ≥ 50% 인 날": 7월 31일 중 중앙값 2일(최소 0, 최대 6), 0일인 동 21개.
- valid pixel 이 조금이라도 있는 날은 모든 동에서 1일 이상(중앙값 4일).
- 장마철 결과이며, 구름 외에 **cloud shadow 플래그가 부분적으로 맑은 날 covered 의 30–60%** (월 평균 0.11–0.12)를 차지해 valid 를 크게 줄임.
  shadow 규칙을 빼면 서울 valid 평균이 0.01–0.05 → 0.11–0.17 로 늘어남(진단용 계산, 산출물엔 미반영). 과다 탐지 여부는 검증 필요.
- 위치 확인: `docs/figures/silver_check_VJ109GA_20240704.png` (확인용 WGS84 표시; 한강 저반사 띠가 강변 행정동 경계를 따라감).

## 한계·미해결
- 행정동 경계 버전/기준일 미확인 → `admin_boundary_version = unknown`.
- 촬영시각은 일 단위. `overpass_window_*` 는 그날 서울 상공 주간 L1B granule 시각 범위(1~2개, 약 03:18–05:42 UTC). 픽셀별 실제 궤도 구분은 미구현.
- NOAA-21 파일은 2025-11 재처리본 → 당시 가용시각 아님(회고 분석).
- 지수는 수역·식생 상태 표현이며 강수량·피해 Label 이 아님. 사후(post-event) 변화지표를 만들 경우 예측 입력에서 제외.
- 1km 지수(MNDWI 등)는 동당 pixel 1개 내외(중앙값) → 동 간 차이보다 pixel 경계 효과가 클 수 있음. `n_pixels_touched_*`, `total_px_*` 함께 사용.
