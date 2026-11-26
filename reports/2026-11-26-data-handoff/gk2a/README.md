# GK2A IR112 기존 자료 인계

이 폴더는 **기존 로컬 본 프로젝트 자료**의 원본 목록과 수치 사용 전 점검 결과다. 신규 수집, 재크롭, 보간, 온도 변환, CNN 변경은 수행하지 않았다. `status: pass`는 아래 무결성·구조·DN 분류 검사에 대한 결과이며 구름 제거 또는 과학적 정확성 인증이 아니다.

## 실제 보유 범위

| 항목 | 확인 결과 |
|---|---|
| 위성·센서·변수 | GK-2A / AMI / LE1B IR112 / `image_pixel_values`, 중심 파장 11.2 μm |
| 원본 | NetCDF 736개, 751,067,060 bytes. 731개 일별 선택 관측 + 추가 관측 5개 |
| 선택 관측 날짜 | KST 2024-01-01–2025-12-31, 731일. 날짜 누락·중복 없음 |
| 기존 Silver | 서울 425개 동의 `admin_crop.tif` + `quality_mask.tif`, 총 850개, 10,061,180 bytes |
| 시간 구조 | 동별 TIFF의 731개 band. 한 band는 한 원본 관측이며 동별로 같은 band index를 사용 |
| 원천 격자 | 900×900, native LCC, 2,000×2,000 m. 서울 전체를 감싸는 원천 창은 16행×19열 |
| 수치 단위 | 16-bit 저장 공간의 유효 13-bit **packed DN**, scale=1, offset=0. Kelvin 아님 |
| 제공기관 | 기상청 NMSC, KMA API Hub GK2A LE1B IR112 KO endpoint. 인증정보 없이 endpoint만 기록 |
| 경계 | `config/hangjeongdong_서울특별시.geojson`, `adm_cd2` 10자리 행정동 코드. 경계 유효 기간은 미확인 |

731회는 본 프로젝트가 선택한 일별 표본이다. 위성의 2분 관측 주기를 모두 확보한 자료가 아니다. 원본 보유 시각을 전체 2분 격자로 확장하거나 없는 관측을 0으로 채우지 않는다.

## 파일과 결합 키

| 파일 | 한 행의 단위·사용 방법 |
|---|---|
| [original_inventory.csv](original_inventory.csv) | 실제 원본 1개. 센서·채널, UTC/KST, 원천 endpoint·제품명, 상대 경로·SHA-256, dtype/nodata, CRS/affine, native/WGS84 범위, 단위·검사 수준 |
| [cleaned_observations.csv](cleaned_observations.csv) | 실제 원본 1개, 736행. 선택된 731개는 기존 Silver의 모든 동에서 읽은 DN 분류·통계. 추가 5개는 Silver 미선택 상태와 결측을 명시 |
| [silver_raster_inventory.csv](silver_raster_inventory.csv) | 동 1개, 425행. image/mask 상대 경로·SHA-256, band/shape/CRS/affine/범위, 원천 행열, 경계 해시 |
| [quality_summary.json](quality_summary.json) | 집계 결과, 실제 시각 예외, 누락·중복·단위 검사, 입력 파일/코드 해시 및 검사 범위 |

경로 필드는 모두 저장소 루트 기준이다. `source_path`로 원본 목록과 정리 관측표를 연결한다. `silver_band_index`를 기존 [observations.csv](../../../data/silver/gk2a/IR112_KO/observations.csv)의 `band_index` 및 TIFF band(1부터 시작)에 연결한다. 동 식별자는 [crop_index.csv](../../../data/silver/gk2a/IR112_KO/crop_index.csv)의 `admin_dong_code`다.

이미 존재하는 동×관측 310,675행의 분류 통계는 [observation_qc.csv.gz](../../../data/silver/gk2a/IR112_KO/observation_qc.csv.gz)를 재사용한다. 키는 `(admin_dong_code, band_index)`이며 `valid_pixel_count`, `source_nodata_count`, `upper_bits_set_count`가 있다. 이번 검사는 이 통계 전부를 TIFF/mask 실제 값과 대조했으며 같은 표를 중복 생성하지 않았다. 처리 계보는 기존 [processing.json](../../../data/silver/gk2a/IR112_KO/processing.json)에 보존돼 있다.

## 시각과 결측

관측 시각은 NetCDF의 `NC_GLOBAL#file_name` 제품 시각을 읽고 정규화 manifest의 UTC/KST와 대조했다. 이는 제품 관측 시각이며 픽셀별 정확한 노출 시각을 뜻하지 않는다. 파일 수정 시각·다운로드 로그의 실행 시각으로 대체하지 않았다. 다운로드 시각은 확정 근거가 부족하여 빈칸과 사유를 남겼다.

선택된 731개 중 727개는 00:00 UTC(09:00 KST)다. 다음 4개는 실제 시각을 그대로 사용한다.

| KST 날짜 | 실제 UTC | 실제 KST |
|---|---|---|
| 2024-05-09 | 2024-05-09 02:00 | 2024-05-09 11:00 |
| 2024-11-07 | 2024-11-06 23:58 | 2024-11-07 08:58 |
| 2025-05-13 | 2025-05-12 23:58 | 2025-05-13 08:58 |
| 2025-11-06 | 2025-11-05 23:58 | 2025-11-06 08:58 |

추가 원본은 2024-05-09 03:00/06:00/12:00 UTC, 2025-04-28 05:00 UTC, 2025-12-31 03:00 UTC다. 보유 원본에는 있으나 기존 731-band Silver에는 없다. 이 5행의 `silver_status`는 `bronze_only_not_selected_for_silver`, `missing_silver_dong_band_count`는 각각 425이며 DN 통계는 빈칸이다. 이는 일별 대표 관측의 날짜 누락과 구분한다.

CSV의 빈 수치 셀은 미제공/미관측이다. `0`은 실제 검사한 개수가 0임을 뜻한다. 미선택 5행의 통계를 0으로 채우거나 다른 band의 값을 복사하지 않았다.

## 변수·마스크 해석

| mask | 의미 | 수치 통계 사용 |
|---|---|---|
| 0 | 해당 동 밖. image 값은 65535 | 제외 |
| 1 | 유효 13-bit DN. 유한값이고 0≤DN≤8191인지 검사 | 포함 |
| 2 | 원천 nodata. image 값은 65535 | 제외 |
| 3 | 상위 비트 설정. 원시 DN 보존 | 제외. 지원하지 않는 코드를 온도로 해석하지 않음 |

`outside_pixel_membership_count`, `source_nodata_pixel_membership_count`, `upper_bits_pixel_membership_count`는 위 분류별 개수다. `nonfinite_inside_pixel_membership_count`는 동 내부 nonfinite 진단이며 마스크 분류와 별도로 검사한다. 알 수 없는 mask 코드는 `invalid_mask_pixel_membership_count`로 집계하고 오류 처리한다. `missing_silver_dong_band_count`는 원본이 실제 동별 band와 연결되지 않는 경우다.

선택 관측 전체에서 실제 mask 0은 **88,451**, mask 1은 **888,896**, mask 2/3·nonfinite·알 수 없는 mask·누락 band는 **각 0**이다. 유효 DN 범위는 **2,569–7,538**이다. 이 결과는 서울 Silver에 한정되며 KO 원본 전체 장면의 nodata/QA 재계산 결과는 아니다.

경계 처리는 `all_touched`이므로 하나의 원천 픽셀이 인접 동에 반복 포함될 수 있다. `*_pixel_membership_count`, `dn_mean_over_dong_memberships`는 동별 픽셀 소속을 합친 값이다. 고유 픽셀 수·서울 면적 평균·지상 coverage로 해석하지 않는다. `valid_dn_membership_fraction`의 분모는 동 내부 소속의 합이며 구름 없는 비율이 아니다.

DN 유효성은 구름·그림자 통과를 뜻하지 않는다. `temperature_k`, `cloud_fraction`은 비어 있으며 구름 QC, 온도 보정, 강수 정답 결합, 지상 면적 coverage는 미완료다. 밝은/어두운 미리보기 색상은 물리적 온도를 제공하지 않는다.

## 공간 정보와 검증 수준

NetCDF를 GDAL로 열면 CRS가 없고 identity affine이 표시된다. 그것을 위치 정보로 사용하지 않고, 기존 `crop_gk2a_silver.grid()`가 원본 projection/pixel-center 태그에서 복원하는 LCC와 pixel-edge affine을 사용했다. 전체 KO affine은 `[2000,0,-900000,0,-2000,900000]`, native bounds는 `[-900000,-900000,900000,900000]` m다. 각 동 TIFF의 CRS·원점·해상도는 실제 메타데이터와 원천 행열을 대조했다. WGS84 envelope는 native 외곽을 좌표 변환한 경계 상자이며 행정동 polygon 자체가 아니다.

기존 검사에 stat snapshot이 없어 현재 파일의 변경 여부를 크기만으로 확정하지 않았다. 원본 736개와 image/mask 850개 전체 바이트 SHA-256을 새로 계산하여 기존 manifest/crop index와 대조했다. 모든 원본을 열어 채널·형상·비트·단위·투영·제품 시각을 검사했고, 작은 Silver/mask는 모든 band 픽셀을 읽어 값·분류·경계 밖 nodata 및 기존 QC 310,675행을 확인했다. 원본의 전체 KO 픽셀 통계는 다시 계산하지 않았으며 `metadata_*_dn`은 원천 메타데이터 값이다.

원본 경로·제품명·SHA-256·관측 시각의 중복은 없다. 경계·원천 목록·기존 처리 코드의 SHA-256을 `processing.json` 계보와 대조했다. 원래 경계 위치 기준 확인은 기존 `processing.json`에 남은 KMA reference 35개 픽셀 중심 검사 기록이며 이번에 외부 reference를 재다운로드한 것은 아니다.

## 재현

저장소 루트에서 기존 Python 환경의 `numpy`, `rasterio`로 실행한다. 네트워크·인증정보는 필요 없다.

```bash
.venv/bin/python scripts/gk2a_delivery_inventory.py --self-test
.venv/bin/python scripts/gk2a_delivery_inventory.py
```

기본 출력은 이 폴더의 CSV 3개와 JSON 1개다. 원본·기존 Silver·기존 manifest는 읽기 전용으로 사용한다. 결과에 실행 시각이나 절대 사용자 경로를 넣지 않아 동일 입력·라이브러리에서 같은 바이트를 재생성할 수 있다. 오류가 있으면 JSON의 `errors`를 남기고 종료 코드 1을 반환한다. 파일 자체를 열 수 없거나 필수 메타데이터가 없는 경우에도 실행이 실패하므로 완료로 취급하지 않는다.
