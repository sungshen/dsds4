# VIIRS Bronze (추가 보조 원천)

> VIIRS 는 4조 가이드라인에 명시되지 않은 **추가 보조 원천**이다. 국토위성(주)·천리안·Sentinel-2 경로를 대체하지 않는다.
> 기간 2024-01 ~ 2025-12 는 검색 범위(가이드 범위 2023–2025 의 부분집합). 다운로드는 2024-07 ~ 2024-12 (536 granule) 수행.

## 원천
| source_dataset | 위성 | CMR concept | 비고 |
|---|---|---|---|
| VNP09GA.002 | Suomi-NPP | C2631841556-LPCLOUD | L2G 일별 지표반사도, sinusoidal tile h28v05 |
| VJ109GA.002 | NOAA-20 | C2631841524-LPCLOUD | 〃 |
| VJ209GA.002 | NOAA-21 | C2817354878-LPCLOUD | 〃 |

- 검색 AOI: `config/hangjeongdong_서울특별시.geojson` 의 전체 범위(bbox, WGS84). 구간 `[월초, 다음월초)` UTC, paging 끝까지, 구름으로 제외하지 않음.
- 파일: granule 당 `.h5`(데이터) + `.cmr.xml`(원천 metadata) 그대로 저장. `data/viirs/<product>/<YYYY-MM>/bronze/originals/<product_id>/`.
- 인증: Earthdata 토큰. 코드·표에 없음. 로컬 실행 시 `EARTHDATA_TOKEN` 환경변수(.env, 커밋 금지).

## 실행
```bash
python scripts/viirs_search.py        # tables/search_results.csv, search_log.csv, data/viirs/bronze/search_raw/
python scripts/viirs_download.py      # config download_months 의 원본 -> asset_manifest.csv, download_log.csv
python scripts/viirs_calendar.py      # observation_calendar.csv (결측일 원인: L1B 존재 여부 CMR 조회)
python scripts/viirs_bronze_check.py  # bronze_check.txt, granule_metadata.csv
```

## 원본 로컬 보관 (클라우드 디스크 한도)
- 클라우드 작업 환경 디스크(약 22 GB)가 6개월 원본(약 41 GB)보다 작아, 월 단위로 다운로드 → Silver → 검증 후
  디스크가 부족할 때만 가장 오래된 처리 완료 월의 `.h5` 를 삭제했다 (`scripts/viirs_release_originals.py`).
- 삭제분: 2024-07·08·09 `.h5` 262개. `asset_manifest` 에 `local_retained=False`, `released_at`, `release_reason` 기록.
  sha256·`.cmr.xml`·`granule_metadata` 는 보존. `viirs_download.py` 로 재다운로드 후 sha256 일치를 확인할 수 있다.
- 클라우드 환경 자체가 세션 종료 시 사라지므로, 원본의 영구 보관은 로컬/외장 디스크 재다운로드로 한다.

## 알려진 한계
- **촬영시각 정밀도 = 일(day)**: L2G 일 합성이라 CMR/파일에 픽셀별 촬영시각이 없음. 궤도번호(`orbit_numbers`)만 있음.
- **제품 가용시각 미상**: `production_time` 은 현재 공개 버전의 생산시각. VJ209GA 2024-07 은 2025-11 재처리본(지연 460~488일)이라 당시 실시간으로 쓸 수 있던 버전이 아님 → Gold 는 회고 분석.
- 타일 수준 `PercentCloudy` 는 h28v05 전체(약 1,100 km) 값이며 서울 값이 아님. 제외 기준으로 쓰지 않음.
- 동일 날짜 복수 granule(VJ209GA 2024-12-11 3개, 2025-10-01 2개)은 재처리 버전으로 보이며 모두 보존.
