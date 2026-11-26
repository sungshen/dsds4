# 서버·플랫폼 운영

**현재 상태(2026-11-26):** VIIRS 처리는 11월 25일 정상 완료됐고, 백업 검증 후 Hetzner 서버·IP를 삭제했습니다. R2와 로컬 플랫폼을 사용하며 상시 서버·SQL은 필요하지 않습니다. [자료 인계서](DATA_HANDOFF.md)에서 최종 목록과 공유 방법을 확인하세요. 아래 서버 배치 명령은 필요할 때 새 환경에 재현하는 기록입니다.

## 역할과 설정

기존 Hetzner `/root/viirs`는 NASA 수집·Silver·Gold 생성 전용이었습니다. R2가 원본과 파생 결과의 저장 기준이며 플랫폼은 Mac의 루프백 주소에서 R2를 조회합니다. 1 GB 서버에 별도 PostgreSQL이나 플랫폼 프로세스를 추가할 필요가 없습니다.

서버 `.env`에는 R2 쓰기 권한과 `EARTHDATA_TOKEN`을 둡니다. Mac의 저장소 `.env` 또는 `platform/.env`에는 R2 읽기 권한과 선택적인 SSH 설정을 둡니다. 빈 플랫폼 설정은 루트 설정을 지우지 않습니다. 환경변수 > 플랫폼의 값 있는 설정 > 루트의 값 있는 설정 순서입니다.

SSH에는 서버 IP, `root`, 이 Mac의 개인키 경로가 필요합니다. 웹 계정 비밀번호와 서버 인증은 별개입니다. 현재 서버의 root SSH는 공개키 인증을 사용합니다. 공개키 fingerprint만으로 접속할 수는 없습니다. 검증된 호스트키를 `known_hosts`에 보관하며 호스트키 검증을 끄지 않습니다. 비밀번호·개인키 내용은 보고서와 Git에 저장하지 않습니다.

## 서버 배치

운영 파일은 `viirs_pipeline.py`, `viirs_s3.py`, `viirs_full_rebuild.py`, 의존성 파일과 `boundaries/`입니다. 저장소의 통합 실행기는 `viirs-full-rebuild-hetzner/` 안에 있지만 서버에서는 공통 모듈과 같은 폴더에 둡니다. 수정할 때는 먼저 로컬 회귀 검사와 서버 Python 컴파일을 통과시키고 원본 코드·보고서를 백업합니다. 실행 중인 파일을 바로 덮어쓰지 않습니다.

```bash
cd /root/viirs
.venv/bin/python -m pip install -r requirements-viirs-s3.txt
.venv/bin/python -m py_compile viirs_full_rebuild.py viirs_pipeline.py viirs_s3.py
systemd-analyze verify /etc/systemd/system/viirs-full-rebuild.service
```

서비스 정의는 저장소의 [`deploy/viirs-full-rebuild.service`](../deploy/viirs-full-rebuild.service)입니다. 이전 transient 서비스를 중지한 후 이 파일을 `/etc/systemd/system/viirs-full-rebuild.service`로 설치하고 다음을 실행합니다. 다른 VIIRS 쓰기 서비스를 함께 켜지 않습니다.

```bash
systemctl daemon-reload
systemctl enable --now viirs-full-rebuild.service
systemctl status viirs-full-rebuild --no-pager
journalctl -u viirs-full-rebuild -n 40 --no-pager
```

메모리 상한 750 MiB, swap 상한 1 GiB, 계산 스레드 1개입니다. 실패 시 60초 뒤 재시작하며 정상 완료하면 중지됩니다. 재부팅 후 활성화됩니다. 머신의 2 GiB swap과 디스크 여유도 별도 확인합니다. 메모리 상한은 성공 보장이 아니므로 OOM과 반복 오류를 로그에서 확인해야 합니다.

## 진행·복구 확인

```bash
systemctl show viirs-full-rebuild -p ActiveState -p SubState -p ExecMainStatus -p NRestarts -p MemoryCurrent
tail -n 15 /root/viirs/full_rebuild_report/daily_results.csv
cat /root/viirs/full_rebuild_report/summary.json
df -h /
free -m
```

`daily_results.csv`는 재실행 이력을 누적합니다. 파일의 행 수를 완료 날짜 수로 세지 말고 `days/YYYY-MM-DD.json`의 최신 날짜별 상태와 이번 실행의 `summary.json`을 확인합니다.

입력·경계·코드 지문과 세 결과의 완료 표식이 일치하는 날만 건너뜁니다. 새 코드 배포 후 기존 날짜를 재처리하는 것은 계보가 바뀌었기 때문입니다. 기존 Bronze는 검증해 재사용합니다. 실패한 날의 보고서와 원본은 지우지 않습니다. 같은 staging 잠금 외에 타 서버 중복 작업도 없는지 확인합니다.

기존 `HANDOFF.txt`의 정오 한 장면 명령과 구형 `viirs-gold-v1`, `viirs-r2-daily`, `viirs-bronze-all` 작업은 현 운영 명령으로 사용하지 않습니다. 서버 백업에는 과거 코드가 남을 수 있으나 자동 실행하지 않습니다.

전체 기간 완료 판정에는 서비스 종료뿐 아니라 731일의 최종 상태, `ERROR_*` 부재, CMR 무관측 날짜 근거, Silver·Quality·Gold·완료 표식 검사를 함께 확인합니다. 구름 QC와 강수 라벨은 별도 검증입니다.

## 플랫폼과 검사

실행은 [플랫폼 문서](../platform/README.md)를 따릅니다. SQL 서비스는 필요하지 않습니다. 예전 `data/platform/gold.sqlite`와 코드 백업은 로컬 보존 자료이며 현재 Gold 조회에 쓰지 않습니다. 캐시를 없애도 R2 원본은 영향받지 않습니다.

```bash
.venv/bin/python -m unittest discover -s . -p 'test_*.py'
.venv/bin/python -m unittest discover -s platform -p 'test_*.py'
.venv/bin/python viirs_r2_audit.py --start 2024-01-03 --stop 2024-01-04 --deep --out r2_audit_report
```

새 클론의 기상청·국토위성 대용량 자료는 Git에 포함된 메타데이터로 위치와 checksum을 확인합니다. 이력에 있다고 로컬 파일이 존재하는 것은 아닙니다. 비밀값은 `.env.example`을 기준으로 별도 준비합니다.
