# DSDS 관측 데이터 플랫폼

로컬 GK2A raster와 R2 VIIRS Silver·Gold를 기간·행정동별로 검색합니다. Gold는 Hetzner 파이프라인이 게시한 R2 Parquet를 직접 조회합니다. PostgreSQL과 SQLite 연결은 필요하지 않습니다.

## 실행

저장소 루트에서 실행합니다.

```bash
.venv/bin/python -m pip install -r platform/requirements.txt
# 루트 .env에 연결 정보가 있으면 그대로 사용합니다.
# 별도 설정이 필요할 때만 platform/.env.example을 platform/.env로 복사합니다.
PYTHONPATH=platform .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

<http://127.0.0.1:8765>를 엽니다. 로컬 사용자용이며 인터넷 공개 서비스의 인증·접근 제어를 제공하지 않습니다. 서버에서 실행할 경우 SSH 터널로 이 루프백 포트에 접속합니다.

## 설정

`R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`가 필요합니다. `R2_PREFIX` 기본값은 `viirs-daily`입니다. 플랫폼에는 버킷 목록·객체 읽기 권한이면 충분합니다. R2 API 비밀값을 웹사이트 비밀번호나 Bearer 토큰으로 대체하지 않습니다.

프로세스 환경변수 > `platform/.env` > 루트 `.env`의 값 있는 항목 순서로 적용합니다. 빈 예시값은 기존 설정을 지우지 않습니다. `.env`는 Git 제외입니다.

SSH 상태 확인은 `HETZNER_SSH_HOST`, `HETZNER_SSH_USER`, `HETZNER_SSH_PORT`, `HETZNER_SSH_KEY_PATH`를 사용합니다. 개인키는 이 Mac에 두고 검증한 서버 호스트키는 `known_hosts`에 등록합니다. 서버 비밀번호 방식이 허용된 환경에서만 `HETZNER_SSH_PASSWORD`를 사용합니다. 구형 `id`·`pw` 별칭보다 명시적인 SSH 설정이 우선합니다. SSH 상태 확인은 서버에 처리 작업을 실행하지 않습니다.

## 조회·미리보기

- 로컬 GK2A: 기존 서울 425개 행정동 TIFF·mask와 731개 관측 인덱스를 읽습니다. 단위는 DN이며 구름 QC는 미검증입니다.
- R2 Silver: 날짜별 `all-observations.parquet`를 읽고 같은 날짜·경계의 구형 장면별 파일보다 우선합니다. 전체 날짜 통계와 원천 장면별 통계를 구분합니다.
- 영상: 실제 원천 행·열에 표시합니다. 여러 장면의 같은 행열을 한 영상에 섞지 않습니다. 기본은 관측 픽셀이 가장 많은 장면이며 장면 선택기로 변경할 수 있습니다. 북쪽이 위인 정사 지도는 아닙니다.
- 위치: 고정 시드의 최대 6,000개 픽셀 표본입니다. 전체 행 수·지상 coverage와 다릅니다. 표는 최대 100행입니다.
- Gold: 행정동 × 관측의 실제 R2 통계입니다. 페이지 단위로 읽고 전수 조회 전에는 전체 건수를 추정하지 않습니다. `100+`는 현재 표시한 행보다 결과가 더 있다는 뜻입니다. 재구축으로 목록이 바뀌면 새 조회를 안내합니다.

미리보기는 객체당 최대 128 MiB를 지원하며 저장된 SHA-256을 검증합니다. 캐시는 `data/platform/`에만 둡니다. 행정동으로 Silver를 검색할 때는 파일을 읽어 필터하므로 기간을 31개 파일 이하로 좁혀야 합니다.

`quality/.../completion.json`과 현재 Silver·Quality·Gold의 경로·크기·해시·처리 지문이 모두 일치해야 `verified`입니다. 완료 표식이 없는 구형 결과나 교체 중인 결과는 경고합니다. 이는 게시 일관성 검증이며 구름 QC 완료를 뜻하지 않습니다.

과거 픽셀 복사형 Gold는 집계 테이블과 스키마가 달라 Gold 탭에서 재처리 안내를 표시합니다. 탐색 화면의 관측 상세에서 원래 스키마를 확인할 수 있습니다. 과거 `gold.sqlite`는 로컬 보존 자료이며 사용하지 않습니다. 구형 SQL API와 로컬 Gold 생성 API는 410 안내를 반환합니다.

## 검증

```bash
.venv/bin/python -m unittest test_platform -v
```

데이터 단위·품질 제한은 [데이터 계약](../docs/DATA_CONTRACT.md), 실제 검수 결과는 [검수 기록](../docs/VERIFICATION.md)을 참고하세요. 강수 정답과 Sentinel 보조 자료는 아직 결합하지 않았습니다.
