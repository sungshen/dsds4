# 위성 데이터 변수 정의서

2026-11-26 기준. 파일별 실제 관측 목록과 검증 결과는 [자료 인계서](DATA_HANDOFF.md)에 연결되어 있습니다. CSV의 빈 값과 Parquet null은 결측·미확보·미검증이며 0으로 채우지 않습니다. 행정동 코드와 제품 ID는 문자열로 읽습니다.

## VIIRS Silver

파일 하나는 KST 하루의 여러 원천 관측입니다. 행의 식별자는 `(swath_id, row, column)`이며 경위도는 EPSG:4326입니다. 동일 행열이라도 다른 swath는 다른 관측입니다.

| 변수 | 저장형·단위 | 정의·결측 규칙 |
|---|---|---|
| `dong_code` | string | 해당 연도 SGIS 2Q의 `ADM_CD`; 픽셀 중심이 경계 내부인 동 |
| `row`, `column` | int32, 원천 픽셀 | 원본 swath의 0 기준 행·열; 정사 지도 좌표가 아님 |
| `longitude`, `latitude` | float64, degree | VNP03IMG 지리 좌표 |
| `i05_brightness_temperature_k` | float32, K | I05 LUT로 변환한 밝기온도; 유효 LUT 값이 없으면 null. 지표면 온도·강수량이 아님 |
| `i05_quality_flags` | uint16, bit field | 원천 QA; 비트 필드를 연속형 수치로 해석하지 않음 |
| `swath_id` | string | `Ayyyyddd.HHMM` 장면 식별자 |
| `swath_start_utc` | timestamp[us, UTC] | 원천 장면 시작 시각; 픽셀 노출 시각이 아님 |
| `scan_index` | int32 | 원천 스캔 번호 |
| `scan_start_time_tai93_s` | float64, TAI93 s | 1993 기준 TAI 스캔 시작 시각. UTC epoch 초와 다르며 변환 시 윤초 처리 필요 |

원천 밖·경계 밖 픽셀은 Silver에 포함하지 않습니다. 이 파일만으로 원천 밖과 경계 밖을 구분하는 raster mask를 복원했다고 주장하지 않습니다. 원천 NetCDF와 경계를 함께 보존하며 재격자화·관측 간 mosaic을 하지 않습니다.

## VIIRS Gold

식별자는 `(date, boundary_version, dong_code, swath_id)`입니다. 통계 포함 조건은 **유한 온도 AND QA=0**입니다. 구름 제거 완료 조건과 구분합니다.

| 변수 | 저장형·단위 | 정의·결측 규칙 |
|---|---|---|
| `date` | date32, KST 일자 | 장면 시작 시각을 KST로 바꾼 날짜 |
| `boundary_version` | string | `sgis-2024-2q` 또는 `sgis-2025-2q` |
| `dong_code`, `swath_id`, `swath_start_utc` | Silver와 같음 | 동·장면·관측 시작 식별자 |
| `pixel_count` | int64, pixel records | 해당 동·장면에 포함한 Silver 행 수; 지상 고유 면적이 아님 |
| `finite_temperature_count` | int64 | 온도가 유한한 픽셀 수; QA 포함 조건과 별도 |
| `flagged_count` | int64 | QA가 0이 아닌 픽셀 수; 구름 픽셀 수가 아님 |
| `valid_count` | int64 | 유한 온도이면서 QA=0인 픽셀 수 |
| `temperature_mean_k`, `temperature_min_k`, `temperature_max_k` | float64, K | 유효 픽셀의 평균·최저·최고; `valid_count=0`이면 null |
| `valid_fraction` | float64, 0–1 | `valid_count / pixel_count`; 분모 0이면 null |
| `first_scan_start_time_tai93_s`, `last_scan_start_time_tai93_s` | float64, TAI93 s | 동·장면에 포함된 스캔 시작 시각의 최소·최대; 관측 종료/픽셀 노출 시각이 아님 |

`valid_count <= finite_temperature_count <= pixel_count`, `valid_count <= pixel_count - flagged_count`를 만족해야 합니다. 온도 이상치를 임의 임계값으로 삭제하지 않습니다. 온도 범위·과학적 이상치 검증이 실시되지 않은 범위는 검증 보고서에서 별도로 표시합니다.

Quality JSON은 픽셀 합계·원천 장면·제품 이름·촬영 메타데이터·코드 해시를 기록합니다. `completion.json`은 Silver·Quality·Gold의 경로·크기·SHA-256·처리 지문을 연결합니다. SHA-256은 원천 표기에 따라 base64와 hex를 구분합니다. 메타데이터 일치 검사와 전체 바이트 해시 검사는 다른 검증 단계입니다.

## GK2A IR112

Bronze는 KO 영역 900×900, 2,000 m LCC 격자의 `uint16 image_pixel_values`입니다. Silver는 서울 425개 동마다 `admin_crop.tif`와 `quality_mask.tif`가 있으며, **band는 날짜순 관측**입니다. IR112의 파장 band 731개라는 뜻이 아닙니다.

| 변수·파일 | 형·단위 | 정의·결측 규칙 |
|---|---|---|
| `image_pixel_values`, `admin_crop.tif` | uint16, packed DN | 이번 처리에서 물리량 변환을 적용하지 않은 저장 코드; Kelvin으로 읽거나 VIIRS 온도와 직접 비교하지 않음 |
| `band_index` | int, 1부터 시작 | `observations.csv`의 실제 관측 시각과 연결 |
| `observation_time_utc`, `observation_time_kst` | ISO 8601 | 각각 UTC와 +09:00. 파일의 실제 시각을 유지하며 모두 09:00으로 덮어쓰지 않음 |
| `date_kst` | YYYY-MM-DD | 한국 날짜; UTC 파일명 일자와 다를 수 있음 |
| `admin_dong_code` | string, 10자리 | 경계의 `adm_cd2`. SGIS 8자리 코드와 문자열 조인 금지 |
| `image_nodata` | uint16, 65535 | 영상의 nodata. 실제 DN 0과 구분 |
| mask 0 | uint8 | 행정동 밖 |
| mask 1 | uint8 | 형식상 유효 13-bit DN; 구름 없음의 증거가 아님 |
| mask 2 | uint8 | 원천 nodata |
| mask 3 | uint8 | 상위 비트 설정. 원시 코드는 보존하되 유효 통계에서 제외 |
| `valid_pixel_count` | int | 동·관측에서 mask=1인 픽셀 수 |
| `source_nodata_count`, `upper_bits_set_count` | int | 각각 mask=2, mask=3의 수 |
| `geometry_pixel_count` | int | `all_touched`로 해당 동에 배정된 원천 픽셀 수 |
| `source_row`, `source_col` | int | 원천 격자에서 crop 시작 행·열 |
| `sha256`, `image_sha256`, `quality_mask_sha256` | hex string | 원본·raster·mask 바이트의 SHA-256 |

분모는 해당 동의 `geometry_pixel_count`입니다. `all_touched` 때문에 이웃 동이 같은 원천 픽셀을 공유하므로 동별 수를 더해 서울의 고유 픽셀 수로 쓰지 않습니다. 경계 유효 기간은 확인되지 않았고 파일 해시로 버전을 고정합니다.

## NGII ARD

원본 관리대장과 `_Aux.xml` 메타데이터의 정의입니다. 실제 영상 파일이 없는 환경에서는 메타데이터를 읽었다는 이유로 band·픽셀·구름 검수를 완료로 표시하지 않습니다.

| 변수 | 단위·정의 | 주의점 |
|---|---|---|
| `catalog_id` | 제공기관 제품 식별자 | 같은 도엽·촬영일의 처리 버전과 구분 |
| `event_id` | E1–E7 | 협업팀과 아직 합의하지 않은 저장소 내 사건 ID |
| `phase` | 사건 전/중/후 | 사건 기간과 촬영일 비교 결과. 피해 발생 시각의 정밀 판단이 아님 |
| `map_number` | string | 1:5,000 도엽 번호 |
| `acquisition_date`, `create_date` | YYYYMMDD | 촬영일과 처리일을 구분; 처리일로 재난 전후를 판단하지 않음 |
| `imaging_center_utc` | 원천 UTC 문자열 | 첫 입력 영상의 중심 시각. 타일 전체의 정확한 노출 시각이 아님; 7자리 소수 원문 보존 |
| `satellite`, `sensor`, `product_level` | CAS500-1 / AEISS-C / Level3A | 제공기관 표기 |
| `crs_epsg`, `gsd_m` | EPSG:5179 / m | 제공 메타데이터의 2 m product sampling. 모든 센서 band의 본래 해상도를 뜻하지 않음 |
| `width`, `height` | pixel | 메타데이터 영상 크기; 실제 raster 대조 여부는 별도 |
| 영상 네 모서리 좌표 | degree | XML에 제공한 위경도; 새 inventory는 네 모서리를 사용한 bbox를 보존 |
| `input_l2g`, `n_input_images` | 원천 제품 목록·개수 | ARD 제작에 사용된 관측의 계보 |
| `incidence_deg`, `off_nadir_deg`, `sun_azimuth_deg`, `sun_elevation_deg` | degree | 첫 입력 영상의 촬영·태양 각도 |
| `atmospheric_correction` | 제공 처리 알고리즘 | 자체 대기·구름 QC 완료와 구분 |
| XML의 band·mask·품질 값 | 원문과 원천 단위 | `PerPixelPercentage`라는 이름만으로 값에 100을 곱하거나 퍼센트로 확정하지 않음 |
| `zip_sha256` | hex string | 다운로드 당시 원본 zip 체크섬; 현재 로컬 바이트 재검증과 구분 |

반사도 scale/offset·밴드별 단위·실제 nodata·구름/그림자·위치 검증이 확보되지 않은 항목은 unknown/unverified로 둡니다. 이 자료로 NDVI 등 지수·피해 통계를 임의 계산하지 않습니다.

## 사건·공간 결합

수치 관측과 위성 관측은 기존 주기를 유지하고 사건 ID·사건 구간·대상 공간으로 추출합니다. 미래 피해 예측용 30 mm/h 강수 라벨은 공식 피해 규모 정답을 대신하지 않습니다.

VIIRS SGIS 8자리, GK2A `adm_cd2` 10자리, NGII 2021-04 vuski 경계는 서로 다른 코드/버전입니다. 현재 변환표가 없으므로 단순 문자열 변환·앞자리 잘라내기로 결합하지 않습니다. 협업 시 공통 경계와 시군구/동 단위를 합의한 뒤 면적 교차 또는 공인 변환표를 기록합니다. 재표본화가 필요하면 CRS·범위·원점·sampling·보간 방법을 산출물별로 적습니다.
