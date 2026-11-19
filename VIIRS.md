# 2024–2025 대한민국 VIIRS 파이프라인

**Bronze**는 한국시간 2024-01-01부터 2026-01-01 직전까지 대한민국 주변을 통과한 Suomi NPP VIIRS Collection 2 관측을 수집합니다. CMR에서 경도 124–132°, 위도 32–39°와 교차하는 장면을 검색하고 관측 시작 시각이 일치하는 VNP02IMG 영상과 VNP03IMG 지리좌표를 묶습니다. 원본 NetCDF는 `data/bronze/viirs/YYYY/MM/DD/`에 보존합니다. 하루 경계와 저장 경로는 KST 기준이며 파일명의 관측 시각은 UTC입니다.

**Silver**는 해당 연도의 전국 행정동·행정읍면 경계로 픽셀 중심을 분류합니다. 경계 내부 픽셀만 원본 스캔 행·열, 위경도, 행정동 코드, I5 밝기온도(K), NASA 품질 플래그와 함께 저장합니다. 결측 밝기온도는 null입니다. `data/silver/viirs/<경계버전>/YYYY/MM/DD/`의 장면별 Parquet에서 `dong_code`로 동별 데이터를 조회할 수 있습니다. 경계선에 정확히 놓인 중심점은 제외합니다. 좌표 재격자화와 보간은 수행하지 않습니다.

**Gold 계획**은 Silver에서 행정동별 I5 적외선 밝기온도와 관측 품질을 요약하고, 수치팀의 일별 강수 자료를 한국시간 날짜·행정동 코드·경계 버전으로 결합하는 것입니다. 실제 촬영 시각은 원본 추적용으로 Silver 메타데이터에 보존합니다.

## 실행

Python 환경에 의존성을 설치합니다. 목록 조회는 인증 없이 실행할 수 있습니다.

```powershell
python -m pip install -r requirements-viirs.txt
python viirs_pipeline.py bronze --inventory
```

[NASA Earthdata Login](https://urs.earthdata.nasa.gov/)에서 발급받은 다운로드 토큰을 저장소 루트의 `.env`에 `EARTHDATA_TOKEN=...` 형식으로 저장하거나 환경 변수로 설정합니다. 다운로드가 중단되면 동일한 명령을 다시 실행합니다. `--start`와 `--stop`으로 기간을 줄일 수 있으며 종료일은 제외됩니다. 원본 전체는 약 1.09 TiB입니다. 로컬 저장 공간이 부족하면 아래 S3 방식을 사용합니다.

```powershell
python viirs_pipeline.py bronze --start 2024-01-01 --stop 2024-01-02
```

SGIS 전국 행정읍면동 경계의 기준일은 각 연도 6월 30일입니다. 2024년 경계 3,558개와 2025년 경계 3,559개에 각 연도의 관측을 연결합니다. `ADM_CD`는 8자리 SGIS 코드입니다. SHP는 SHX, DBF, PRJ와 같은 폴더에 둡니다. 경계 스냅샷을 바꿀 때 새 경계 버전을 지정합니다. [2024년 배포 이력](https://data.edmgr.kr/dataView.do?id=www-data-go-kr-data-filedata-15129688) · [2025년 공공데이터포털 배포본](https://www.data.go.kr/data/15129688/fileData.do)

```powershell
python viirs_pipeline.py silver --year 2024 --boundary .venv/viirs-boundaries/2024/bnd_dong_00_2024_2Q.shp --code-column ADM_CD --boundary-version sgis-2024-2q
python viirs_pipeline.py silver --year 2025 --boundary .venv/viirs-boundaries/2025/bnd_dong_00_2025_2Q.shp --code-column ADM_CD --boundary-version sgis-2025-2q
```

## AWS S3

서울 리전(`ap-northeast-2`)에 범용 비공개 버킷을 만들고 기본 암호화(SSE-S3)를 사용합니다. 사용할 AWS CLI 프로필에는 해당 버킷의 `s3:ListBucket`와 `viirs/*` 객체의 `s3:GetObject`, `s3:PutObject` 권한이 필요합니다. 조직에서 IAM Identity Center를 사용하면 SSO 프로필로 로그인합니다. 그 외에는 AWS CLI v2의 콘솔 로그인으로 임시 인증할 수 있습니다. [S3 설정](https://docs.aws.amazon.com/AmazonS3/latest/userguide/GetStartedWithS3.html) · [AWS CLI 로그인](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html) · [AWS CLI SSO](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)

```powershell
aws login --profile viirs
python -m pip install -r requirements-viirs-s3.txt
```

첫날을 실행해 버킷 접근과 결과를 확인합니다. `my-viirs-bucket`을 실제 버킷 이름으로 바꿉니다. 한 번에 한 관측 장면의 원본 두 파일과 Silver 결과만 로컬 임시 디렉터리에 둡니다. S3 객체는 `viirs/bronze/`와 `viirs/silver/` 아래에 저장됩니다. 원본만 약 1,115 GiB이며, 서울 S3 Standard의 첫 구간 단가로 한 달 내내 보관할 경우 스토리지 요금은 약 US$28입니다. Silver, 요청, 데이터 반출 요금은 별도입니다. [AWS S3 요금](https://aws.amazon.com/s3/pricing/) · [서울 리전 요금표](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/ap-northeast-2/index.json)

```powershell
python viirs_s3.py --year 2024 --start 2024-01-01 --stop 2024-01-02 --bucket my-viirs-bucket --profile viirs --region ap-northeast-2 --boundary .venv/viirs-boundaries/2024/bnd_dong_00_2024_2Q.shp --code-column ADM_CD --boundary-version sgis-2024-2q
```

전체 연도를 수집하려면 `--start`와 `--stop`을 생략합니다. 2025년에는 `--year 2025`, 2025년 경계 파일, `--boundary-version sgis-2025-2q`를 지정합니다. 전송이 중단되면 동일한 명령으로 재개합니다. AWS 로그인 세션이 만료됐을 때는 먼저 다시 로그인합니다.

## Cloudflare R2

운영 수집은 `--one-per-day`를 사용합니다. 한국시간 정오에 가장 가까운 장면의 영상·좌표 한 쌍을 선택하고, 쌍을 확보할 수 없는 날짜는 로그에 기록합니다. `--stage-dir`로 원본 한 쌍을 임시로 둘 서버 디스크 경로를 지정할 수 있습니다. R2 Standard 저장량은 계정 기준 날짜별 최대값을 평균해 계산합니다. [R2 Standard 요금](https://developers.cloudflare.com/r2/pricing/)

R2 Overview에서 비공개 Standard 버킷을 만들고, Account Details의 API Tokens 메뉴에서 해당 버킷으로 범위를 제한한 Object Read & Write 토큰을 발급합니다. 저장소 루트 `.env`에 기존 `EARTHDATA_TOKEN`과 함께 다음 값을 추가합니다. [R2 버킷 설정](https://developers.cloudflare.com/r2/get-started/) · [R2 인증](https://developers.cloudflare.com/r2/api/tokens/)

```dotenv
R2_ACCOUNT_ID=Cloudflare_계정_ID
R2_BUCKET=버킷_이름
R2_ACCESS_KEY_ID=Access_Key_ID
R2_SECRET_ACCESS_KEY=Secret_Access_Key
```

첫날 샘플은 `viirs-daily/bronze/`에 원본, `viirs-daily/silver/`에 픽셀 Parquet, `viirs-daily/quality/`에 결측 JSON을 저장합니다. 결측 보고서에는 전체 픽셀 수, 밝기온도 결측, NASA의 중복 픽셀 제거로 인한 결측, 기타 결측, 유효 관측이 없는 행정동 수가 들어갑니다.

```powershell
python -m pip install -r requirements-viirs-s3.txt
python viirs_s3.py --r2 --one-per-day --prefix viirs-daily --year 2024 --start 2024-01-01 --stop 2024-01-02 --boundary .venv/viirs-boundaries/2024/bnd_dong_00_2024_2Q.shp --code-column ADM_CD --boundary-version sgis-2024-2q
```

전체 연도에는 `--start`와 `--stop`을 생략합니다. 2025년 실행 시 연도, 경계 파일 및 경계 버전을 2025년 값으로 바꿉니다.

NASA 제품 문서는 [VNP02IMG](https://ladsweb.modaps.eosdis.nasa.gov/missions-and-measurements/products/VNP02IMG/)와 [VNP03IMG](https://ladsweb.modaps.eosdis.nasa.gov/missions-and-measurements/products/VNP03IMG/)에서 확인할 수 있습니다.
