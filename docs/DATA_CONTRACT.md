# Silver·Gold 데이터 계약

## 공통

원천 밖·행정동 밖·nodata·품질 제외를 유효 관측에 섞지 않습니다. 다른 시각 관측을 같은 관측처럼 평균하거나 mosaic하지 않습니다. 제공기관의 ARD·정사영상 처리와 자체 위치·band·단위·품질 검증을 구분합니다.

비교를 위한 정합이 필요하면 projected CRS·범위·원점·해상도·보간 방법을 별도 산출물에 명시합니다. 현재 VIIRS는 원천 swath 격자를 유지합니다. Sentinel 20 m 보조 자료, SCL, Sentinel 지수는 이 파이프라인에 포함되어 있지 않습니다.

## VIIRS Silver

날짜별 파일은 여러 관측의 픽셀 레코드입니다. 식별 단위는 `(swath_id, row, column)`입니다. 다른 장면의 같은 행열은 같은 지상 위치가 아닙니다.

| 필드 | 의미 |
|---|---|
| `dong_code` | SGIS 행정동 코드. 경계 밖 픽셀은 파일에 없음 |
| `row`, `column` | 원천 swath의 행·열 |
| `longitude`, `latitude` | VNP03IMG 경위도, EPSG:4326 |
| `i05_brightness_temperature_k` | I05 LUT 밝기온도(K), 결측 null |
| `i05_quality_flags` | 원천 QA 비트 |
| `swath_id`, `swath_start_utc` | 장면 ID와 UTC 시작 시각 |
| `scan_index`, `scan_start_time_tai93_s` | 스캔 번호·TAI93 초 단위 스캔 시작 시각 |

스캔 시작은 픽셀의 정확한 노출 시각과 다릅니다. TAI93 초를 UTC 초로 간주하지 않습니다. I05는 밝기온도이며 지표면 온도·가시광 사진이 아닙니다. 경계선 위 픽셀 중심은 제외됩니다. 전체 행 수는 고유 지상 면적이나 하루의 고유 픽셀 수가 아닙니다.

## VIIRS Gold·품질

한 행은 `(date, boundary_version, dong_code, swath_id)`입니다. Gold `pixel_count` 합은 Silver 행 수와 일치해야 합니다. 유한 온도이며 QA가 0인 픽셀만 `valid_count`와 평균·최저·최고 온도에 포함합니다.

`valid_fraction = valid_count / pixel_count`의 분모는 해당 행정동·장면에 배정된 모든 Silver 픽셀입니다. 유효 픽셀이 없으면 온도 통계는 null입니다. 관측에 포함되지 않은 행정동의 행은 만들지 않습니다. Gold 행 수에는 날짜·장면별 반복 관측이 포함되므로 행정동 개수와 다릅니다.

`finite_temperature_count`, `flagged_count`도 기록합니다. QA 0은 현재 통계 포함 규칙이며 구름·그림자 제거 완료가 아닙니다. 구름 비율과 지상 coverage는 검증 근거·분모가 없으면 null 또는 미검증입니다. 수치팀의 공간·시간 단위를 합의하기 전에는 강수 정답을 생성하지 않습니다.

원천 목록·경계 해시·처리 버전과 코드 해시·SHA-256으로 계보를 연결합니다. 완료 표식은 세 결과의 게시 일관성 기록이며 과학적 정확성·구름 QC 보증이 아닙니다.

## GK2A·NGII

GK2A `admin_crop.tif`는 서울 행정동별 다중 band이며 band는 `observations.csv`와 연결됩니다. `quality_mask.tif`: 0=밖, 1=유효 DN, 2=nodata, 3=상위 비트 설정. DN을 온도(K)로 읽으면 안 됩니다. `all_touched` 처리로 인접 동이 같은 원천 픽셀을 공유할 수 있습니다.

NGII `tables/ngii/`는 다운로드·도엽·메타데이터 관리 자료입니다. 다른 환경의 완료 이력을 보존하며 현재 Mac에 원본이 모두 있다는 뜻은 아닙니다. 자체 Silver 검증과 Gold 결합은 별도 작업입니다.
