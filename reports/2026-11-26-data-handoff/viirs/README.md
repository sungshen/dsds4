# VIIRS 데이터 인계 — 2026-11-26

기존 R2 결과와 종료된 서버의 일별 보고서를 정리한 읽기 전용 스냅샷입니다. 수집·재처리·업로드를 실행하지 않았습니다. 관측 기간은 **KST 2024-01-01 이상, 2026-01-01 미만**입니다.

| 구분 | 결과 |
|---|---:|
| 전체 R2 객체 | 9,796개 / 1,210,288,334,015바이트 |
| Bronze 원본 | 6,952개 |
| Silver / Quality / Gold / 완료 표식 | 각각 711개 |
| 게시 결과가 있는 날짜 | 711일 |
| 원래 CMR 검색에서 제품 쌍이 없었던 날짜 | 20일 |
| 장면 | 3,476개 |
| Silver 픽셀 레코드 | 1,125,551,292행 |
| 유한 온도 | 978,307,320개 |
| QA 비트가 0이 아닌 픽셀 | 151,385,869개 |
| 유한 온도이면서 QA=0인 현재 Gold 포함 표본 | 974,165,423개 |
| Gold 행정동 × 장면 그룹 | 7,043,394행 |

현재 Quality 기록에서 장면별 합계와 날짜별 합계가 일치하고, 3,476개 장면 모두 I05 LUT 단위가 Kelvin으로 선언되어 있습니다. 고유 `(KST 날짜, swath_id)` 중복과 원본 제품·시각·버전의 불일치는 발견되지 않았습니다. 전체 Silver의 `(swath_id, row, column)` 중복 여부를 새로 스캔한 결과는 아닙니다.

장면 567개는 행정동 안에 배정된 픽셀이 0개이고, 592개는 현재 규칙에 맞는 유효 온도 표본이 없습니다. 원본 검색의 BBOX 교차가 육지 관측을 보장하지 않으므로 이 기록을 지우거나 관측값 0으로 바꾸지 않았습니다. 예상 검색 결측 20일 외에 이번 게시·Quality 검사에서 오류나 경고는 발견되지 않았습니다.

## 인계 파일

- `summary.json`: 수량·바이트·QA·단위 확인·검사 범위·제한.
- `objects.csv`: **모든 R2 객체**의 `object_key`, `tier`, `date_kst`, `size_bytes`, `etag`, `sha256` 및 체크섬 근거. 계정·버킷 인증값이나 다운로드 서명은 포함하지 않습니다.
- `days.csv`: 731일의 게시 상태·픽셀/QA 합계·서버에 기록된 Gold 행 수·처리 지문.
- `scenes.csv`: 장면별 위성·센서·관측 UTC/KST·원본 ID/해시·픽셀/QA 수·단위·공간 범위의 근거.
- `source_assets.csv`: 사용한 원본 6,952개의 제품 ID·Collection·원본 격자 크기·변수 단위·관측 시각·계보.
- `lineage_versions.json`: 처리 코드 파일별 해시와 연도별 경계 해시의 두 조합.
- `monthly_summary.csv`: 월별 결과 수·검색 결측·장면·픽셀/QA·Gold 그룹 수.
- `issues.csv`: 예상된 검색 결측과 발견한 이상. `expected_gap`은 데이터 처리 오류와 구분합니다.
- `server_daily_status.csv`, `server_report_evidence.json`: 비밀 설정을 제외한 기존 서버의 고유 날짜별 완료 기록과 백업 SHA-256.
- `list_snapshot.json`: 전체 LIST 시점·ETag·수량의 재현 가능한 스냅샷.
- `sample_head_checks.json`: 처음·중간·마지막 게시일의 세 결과를 HEAD로 확인한 기록.
- `latest_parquet_footers.json`: 마지막 날 Silver·Gold의 **footer 범위만** 읽은 실제 스키마·행 수·열 통계. 해당 날짜 표본의 통계입니다.
- `duplicate_candidates.json`: LIST의 ETag와 크기가 같은 객체 후보. 비어 있으면 이 기준의 후보가 없다는 뜻이며, 전체 바이트의 중복 검증과 다릅니다.

별도 Gold 전체 내용 검증은 상위 인계 폴더의 `gold/` 결과를 함께 확인합니다. 이 폴더의 `CONSISTENT_METADATA_AND_QUALITY`는 게시·Quality 기록의 일관성 상태입니다.

## 검사 수준

전체 9,796개 객체를 먼저 페이지 단위 LIST로 조사했습니다. 711개 완료 표식과 711개 Quality JSON의 실제 본문 SHA-256·메타데이터·처리 지문을 검사하고, 세 파생 객체의 경로·크기를 완료 표식과 대조했습니다. 원본 이름·제품 쌍·Collection·시각·단위·장면별 합계도 확인했습니다. 처음·중간·마지막 게시일의 Silver·Quality·Gold 9개 객체에 대해서는 HEAD의 크기·SHA·지문·버전·ETag를 추가 대조했습니다.

전체 NetCDF·Silver를 다시 내려받거나 다시 처리하지 않았습니다. Silver와 Gold의 날짜별 행 수는 기존 고유 일별 서버 보고서에 기록된 값이며 마지막 날짜만 footer 행 수를 추가 확인했습니다. 전체 Gold 바이트·내용 검사는 별도 `gold/` 보고서의 범위를 따릅니다. 원래 서버의 누적 CSV는 743행 중 2024-01-01~12가 재실행으로 중복되므로 합산하지 않고 **고유 731개 일별 JSON**을 사용했습니다.

## 해석할 때 지킬 점

- 센서는 Suomi NPP VIIRS, 원본은 `VNP02IMG`·`VNP03IMG` Collection 2입니다. I05 결과는 **밝기온도 K**이며 지표면 온도나 강수량이 아닙니다.
- 검색 BBOX `[124, 32, 132, 39]`는 경도·위도 EPSG:4326의 **CMR 검색 조건**입니다. 장면별 실제 육지 coverage나 관측 픽셀 BBOX로 쓰지 않습니다. `measured_*`가 비어 있는 값은 측정하지 않았습니다.
- 원천 픽셀 중심을 연도별 SGIS 행정동에 배정했습니다. 보간·재격자화·mosaic을 하지 않았으며 경계선 위 중심은 제외됩니다. 2024년과 2025년의 연도별 2분기 경계를 해당 연도 전체에 적용합니다.
- `QA=0 AND finite temperature`는 현재 통계 포함 규칙입니다. **구름 QC는 전 기간 `unverified`**이며 구름·그림자 제거, 물리적 온도 범위, 전국 지상 coverage의 검증 완료를 뜻하지 않습니다. QA 제외와 비유한 온도 수는 겹칠 수 있어 두 수를 더해 제외 총수로 사용하면 안 됩니다.
- 여러 장면의 반복 관측을 유지하므로 픽셀 합계는 고유 지상 면적이 아닙니다. Gold 한 행은 행정동 × 장면이며, 관측되지 않은 행정동 행을 만들지 않습니다.
- UTC/KST는 원본 관측 범위 시각입니다. TAI93는 스캔 시작 초 단위이며 정확한 픽셀 노출 시각이나 UTC 초로 간주하지 않습니다.
- 20일 `NO_PAIRED_CMR`은 당시 조건으로 원본 제품 쌍을 찾지 못했다는 뜻입니다. NASA 전체 무관측의 증거가 아니며 0 K·0 강수로 채우면 안 됩니다. 강수 정답·폭우 라벨은 생성하지 않았습니다.

## 재현

저장소 루트에서 기존 읽기 권한 설정을 사용합니다. 서버나 새 수집 작업은 필요하지 않습니다.

```bash
.venv/bin/python scripts/viirs_delivery_inventory.py
```

이 명령은 LIST를 갱신하고, ETag·크기·마지막 변경 시각이 같은 작은 JSON은 비공개 로컬 캐시를 재사용합니다. 새로운 스냅샷은 다른 `--out` 경로로 저장할 수 있습니다. 로컬의 비공개 서버 백업이 없으면 공개된 `server_daily_status.csv`·증거 파일을 재사용합니다.

동일 스냅샷을 네트워크 없이 다시 계산하려면 작은 JSON의 비공개 캐시가 남아 있어야 합니다.

```bash
.venv/bin/python scripts/viirs_delivery_inventory.py --offline
```

오프라인 HEAD/footer 증거도 LIST 스냅샷 SHA-256과 일치해야 합니다. 원본·Parquet 전체 내용 검증을 대신하지 않습니다.


## 기존 장면의 제공 공간 범위

`scene_footprints.csv`는 이미 보관한 VNP02IMG 3,476개 제품 이름을 NASA CMR의 공개 메타데이터에 정확히 대조한 결과다. 35개 소규모 메타데이터 요청으로 3,476건 모두 한 건씩 일치했다. 새 장면 검색·영상 다운로드·R2 변경은 하지 않았다. 제공기관 polygon/box 원문, 좌표 envelope, 원천 granule·Collection·메타데이터 식별자와 조회 근거를 보존한다. `scene_footprints_summary.json`에 건수·응답 해시·검사 범위를 기록했다.

제공 footprint와 원시 좌표의 envelope는 센서 관측 범위의 메타데이터이며, 행정동 안의 실제 Silver 픽셀 footprint·육지 coverage·구름 없는 면적이 아니다. `query_bbox_wsen`과도 구분한다. 다각형 변의 측지선 극값까지 새로 계산한 범위로 해석하지 않는다.

동일 공개 메타데이터의 비공개 캐시가 있을 때는 다음으로 재현할 수 있다.

```bash
.venv/bin/python scripts/viirs_scene_footprints.py --offline
```
