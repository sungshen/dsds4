# Hetzner VIIRS 통합 실행기

기준 문서는 [VIIRS 가이드](../VIIRS.md), [서버 운영](../docs/OPERATIONS.md), [데이터 계약](../docs/DATA_CONTRACT.md)입니다.

`viirs_full_rebuild.py`를 서버 `/root/viirs/viirs_full_rebuild.py`로 배치합니다. 실행 위치에는 공통 `viirs_pipeline.py`, `viirs_s3.py`, `.env`, `boundaries/`, `.venv/`가 필요합니다.

```bash
PYTHONPATH=. .venv/bin/python viirs-full-rebuild-hetzner/viirs_full_rebuild.py --help
```

기본 실행은 2024–2025 KST 전체를 처리하고 R2에 실제 게시합니다. 조회만 하려면 `viirs_r2_audit.py`를 사용합니다. 운영 prefix에 다른 쓰기 작업을 동시에 실행하지 마세요.
