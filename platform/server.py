"""Read-only DSDS explorer; published R2 Parquet is the VIIRS source of truth."""
import base64
import binascii
import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Literal

import boto3
import numpy as np
import pyarrow.parquet as pq
import rasterio
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from raster_preview import build_preview
from ssh_access import connect as connect_ssh

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = ROOT / 'data' / 'platform'
STATE.mkdir(parents=True, exist_ok=True)
app = FastAPI(title='DSDS Data Explorer')
checks = {}
cache_lock = threading.Lock()
index_cache = {}


def config():
    # Reload local settings so configuration can be added while the app is running.
    from dotenv import dotenv_values
    settings = {}
    for layer in (dotenv_values(ROOT / '.env'), dotenv_values(HERE / '.env'), os.environ):
        settings.update({key: value for key, value in layer.items() if value is not None and str(value).strip()})
    settings['HETZNER_SSH_USER'] = settings.get('HETZNER_SSH_USER') or settings.get('id')
    settings['HETZNER_SSH_PASSWORD'] = settings.get('HETZNER_SSH_PASSWORD') or settings.get('pw')
    return settings


def r2_client():
    cfg = config()
    names = ['R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY']
    if not all(cfg.get(k) for k in names):
        raise HTTPException(409, 'platform/.env에 R2 연결 설정을 입력하세요.')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', cfg['R2_ACCOUNT_ID']):
        raise HTTPException(409, 'R2_ACCOUNT_ID 형식을 확인하세요.')
    client = boto3.client('s3', endpoint_url=f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                          region_name='auto', aws_access_key_id=cfg['R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=cfg['R2_SECRET_ACCESS_KEY'],
                          config=Config(connect_timeout=6, read_timeout=30,
                                        retries={'max_attempts': 2}, signature_version='s3v4'))
    return client, cfg['R2_BUCKET'], cfg.get('R2_PREFIX', 'viirs-daily').strip('/')


def opaque(source, key):
    parts = [source, key]
    if source == 'r2':
        cfg = config()
        parts.append(hashlib.sha256(f"{cfg.get('R2_ACCOUNT_ID')}:{cfg.get('R2_BUCKET')}".encode()).hexdigest())
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip('=')


def decode(value):
    try:
        parts = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if not isinstance(parts, list) or len(parts) not in (2, 3):
            raise ValueError()
        source, key = parts[:2]
        if source not in ('local', 'r2') or not isinstance(key, str):
            raise ValueError()
        if source == 'r2' and opaque(source, key) != value:
            raise ValueError()
        return source, key
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise HTTPException(400, '관측 식별자가 올바르지 않습니다.') from None


def validate_dates(start, end):
    try:
        if start:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start):
                raise ValueError()
            date.fromisoformat(start)
        if end:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end):
                raise ValueError()
            date.fromisoformat(end)
        if start and end and start > end:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, '날짜 범위를 확인하세요.') from None


def local_observations():
    path = ROOT / 'data/silver/gk2a/IR112_KO/observations.csv'
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def local_dongs():
    path = ROOT / 'data/silver/gk2a/IR112_KO/crop_index.csv'
    if not path.exists():
        return {}
    with path.open(encoding='utf-8-sig') as stream:
        return {r['admin_dong_code']: r for r in csv.DictReader(stream)}


def local_record(key):
    record = next((r for r in local_observations() if r['observation_time_utc'] == key), None)
    if record is None:
        raise HTTPException(404, '로컬 관측을 찾을 수 없습니다.')
    return record


def r2_index(stage):
    client, bucket, prefix = r2_client()
    cache_key = (bucket, prefix, stage, config().get('R2_ACCOUNT_ID'))
    with cache_lock:
        cached = index_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
    items = []
    path_prefix = f'{prefix}/{stage}/'
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=path_prefix):
        for obj in page.get('Contents', []):
            if not obj['Key'].endswith('.parquet'):
                continue
            relative = obj['Key'][len(path_prefix):]
            match = re.fullmatch(r'(.+)/(\d{4})/(\d{2})/(\d{2})/([^/]+\.parquet)', relative)
            if match is None:
                continue
            version, year, month, day, name = match.groups()
            try:
                date(int(year), int(month), int(day))
            except ValueError:
                continue
            items.append(dict(id=opaque('r2', obj['Key']), source='r2', stage=stage,
                              date=f'{year}-{month}-{day}', observation_time=None,
                              boundary_version=version, name=name, size=obj['Size'],
                              etag=obj.get('ETag'), kind='parquet'))
    # A rebuild briefly leaves both generations in one day folder. Never count
    # those old per-scene files together with their integrated replacement.
    canonical = 'all-observations.parquet' if stage == 'silver' else 'dong-observations.parquet'
    replaced = {(r['boundary_version'], r['date']) for r in items if r['name'] == canonical}
    items = [r for r in items if r['name'] == canonical or (r['boundary_version'], r['date']) not in replaced]
    for item in items:
        item['generation'] = 'all-observations' if item['name'] == canonical else 'legacy-single-scene'
    items.sort(key=lambda r: (r['date'], r['id']))
    with cache_lock:
        index_cache[cache_key] = (time.monotonic(), items)
    return items


def parquet_source(key, *, expected_etag=None):
    # DSDS_EXPLORER_V2_SILVER_GOLD_SAFE: verified disk-backed R2 Parquet cache.
    # This replaces the old 128 MiB cap, but still prevents unbounded downloads.
    client, bucket, prefix = r2_client()
    if not key.startswith((f'{prefix}/silver/', f'{prefix}/gold/')) or not key.endswith('.parquet'):
        raise HTTPException(400, '허용된 Silver/Gold Parquet 경로를 선택하세요.')
    head = client.head_object(Bucket=bucket, Key=key)
    size = int(head.get('ContentLength', 0))
    if not 0 < size <= 1024 * 1024 * 1024:
        raise HTTPException(413, '관측 파일이 비어 있거나 1GiB를 초과합니다.')
    etag = head.get('ETag', '')
    if expected_etag and expected_etag != etag:
        with cache_lock:
            index_cache.clear()
        raise HTTPException(409, 'R2 데이터가 갱신됐습니다. 검색을 다시 실행하세요.')
    checksum = head.get('Metadata', {}).get('sha256')
    cache = STATE / 'parquet_cache'
    cache.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f'{config().get("R2_ACCOUNT_ID")}:{bucket}:{key}:{etag}:{size}'.encode()).hexdigest()
    path = cache / f'{token}.parquet'
    if not path.is_file() or path.stat().st_size != size:
        from uuid import uuid4
        temp = cache / f'{token}.{uuid4().hex}.partial'
        digest = hashlib.sha256()
        actual = 0
        try:
            response = client.get_object(Bucket=bucket, Key=key, IfMatch=etag)
            with response['Body'] as body, temp.open('wb') as out:
                for part in iter(lambda: body.read(4 * 1024 * 1024), b''):
                    if not part:
                        continue
                    actual += len(part)
                    if actual > size:
                        raise HTTPException(502, 'R2 다운로드 크기가 객체 크기를 초과합니다.')
                    out.write(part)
                    digest.update(part)
            sha = digest.hexdigest()
            encoded_sha = base64.b64encode(digest.digest()).decode()
            if actual != size or (checksum and checksum not in (sha, encoded_sha)):
                raise HTTPException(502, 'R2 Parquet 크기 또는 체크섬이 일치하지 않습니다.')
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    # Read the cached file in chunks so the complete payload is never loaded into RAM.
    local_hash = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            local_hash.update(part)
    digest = local_hash.hexdigest()
    if checksum and checksum not in (digest, base64.b64encode(local_hash.digest()).decode()):
        path.unlink(missing_ok=True)
        raise HTTPException(502, '로컬 캐시 체크섬이 일치하지 않습니다. 다시 시도하세요.')
    # Limit retained disk cache. An open Parquet reader remains usable on Unix.
    try:
        candidates = sorted(cache.glob('*.parquet'), key=lambda f: f.stat().st_mtime, reverse=True)
        total_size = 0
        for n, candidate in enumerate(candidates):
            total_size += candidate.stat().st_size
            if candidate != path and (n >= 5 or total_size > 2 * 1024**3):
                candidate.unlink(missing_ok=True)
    except OSError:
        pass
    return pq.ParquetFile(str(path)), digest
def metadata_of(parquet):
    raw = parquet.schema_arrow.metadata or {}
    return {k.decode('utf-8', 'replace'): v.decode('utf-8', 'replace') for k, v in raw.items()}


def quality_report(key):
    if '/silver/' not in key:
        return None
    client, bucket, _ = r2_client()
    report_key = key.replace('/silver/', '/quality/', 1).removesuffix('.parquet') + '.json'
    try:
        response = client.get_object(Bucket=bucket, Key=report_key)
        with response['Body'] as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return None
        report = json.loads(payload)
        return report if isinstance(report, dict) else None
    except (ClientError, BotoCoreError, ValueError):
        return None


def finite(value):
    return value if not isinstance(value, float) or math.isfinite(value) else None



def scene_column(parquet):
    return next((name for name in ('swath_id', 'source_observation_id')
                 if name in parquet.schema_arrow.names), None)


def selected_batches(parquet, *, dong='', swath='', columns=None):
    import pyarrow.compute as pc
    field = scene_column(parquet)
    selected = list(columns) if columns is not None else None
    if selected is not None:
        selected = list(dict.fromkeys([*selected, *(['dong_code'] if dong else []), *([field] if swath and field else [])]))
    for batch in parquet.iter_batches(batch_size=32768, columns=selected):
        if dong:
            batch = batch.filter(pc.fill_null(pc.equal(batch.column('dong_code'), dong), False))
        if swath:
            if not field:
                raise HTTPException(400, '단일 장면 파일에는 장면 필터를 지정하지 마세요.')
            batch = batch.filter(pc.fill_null(pc.equal(batch.column(field), swath), False))
        if batch.num_rows:
            yield batch


def sample_points(parquet, *, dong='', swath='', mapping=None):
    # Independent random priorities give every selected pixel equal probability,
    # including short final batches. Only the best 6000 priorities stay in memory.
    names = ['dong_code', 'longitude', 'latitude']
    field = scene_column(parquet)
    names += ['i05_brightness_temperature_k', 'i05_quality_flags'] if mapping is None else ([field] if field else [])
    if not set(names).issubset(parquet.schema_arrow.names) or (mapping is not None and not field):
        return []
    rng = np.random.default_rng(0)
    kept = None
    priorities = np.empty(0)
    for batch in selected_batches(parquet, dong=dong, swath=swath, columns=names):
        x = batch.column('longitude').to_numpy(zero_copy_only=False)
        y = batch.column('latitude').to_numpy(zero_copy_only=False)
        coords = np.isfinite(x) & np.isfinite(y) & (np.abs(x) <= 180) & (np.abs(y) <= 90)
        batch = batch.filter(coords)
        if not batch.num_rows:
            continue
        ranks = rng.random(batch.num_rows)
        pick = np.argpartition(ranks, -min(6000, len(ranks)))[-6000:]
        import pyarrow as pa
        part = pa.Table.from_batches([batch]).take(pa.array(pick))
        combined = pa.concat_tables([kept, part]) if kept is not None else part
        ranks = np.concatenate([priorities, ranks[pick]])
        select = np.argpartition(ranks, -min(6000, len(ranks)))[-6000:]
        kept, priorities = combined.take(pa.array(select)), ranks[select]
    if kept is None:
        return []
    result = []
    for row in kept.to_pylist():
        if mapping is None:
            value = finite(row['i05_brightness_temperature_k']) if row['i05_quality_flags'] == 0 else None
        else:
            value = mapping.get((str(row['dong_code']), str(row[field])))
        result.append(dict(longitude=row['longitude'], latitude=row['latitude'], value=value))
    return result


def sha_hex(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r'[a-fA-F0-9]{64}', value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.hex() if len(decoded) == 32 else None
    except (ValueError, binascii.Error):
        return None


def publication_info(key):
    """A day is committed only after the manifest matches all three current objects."""
    filename = key.rsplit('/', 1)[-1]
    if filename not in ('all-observations.parquet', 'dong-observations.parquet'):
        return {'status': 'legacy_unverified', 'detail': '이전 단일 장면 산출물 · 전체 관측 재처리 완료 여부 미검증'}
    client, bucket, prefix = r2_client()
    relative = key.split('/silver/', 1)[-1] if '/silver/' in key else key.split('/gold/', 1)[-1]
    day_path = relative.rsplit('/', 1)[0]
    manifest_key = f'{prefix}/quality/{day_path}/completion.json'
    try:
        response = client.get_object(Bucket=bucket, Key=manifest_key)
        with response['Body'] as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('oversized manifest')
        manifest = json.loads(raw)
        if manifest.get('manifest_version') != 1:
            raise ValueError('manifest version')
        fingerprint = manifest['input_fingerprint']
        expected = {'silver': f'{prefix}/silver/{day_path}/all-observations.parquet',
                    'quality': f'{prefix}/quality/{day_path}/all-observations.json',
                    'gold': f'{prefix}/gold/{day_path}/dong-observations.parquet'}
        for label, expected_key in expected.items():
            item = manifest['objects'][label]
            if item['key'] != expected_key or not sha_hex(item['sha256']):
                raise ValueError('manifest path or hash')
            head = client.head_object(Bucket=bucket, Key=expected_key)
            meta = head.get('Metadata', {})
            if (int(head['ContentLength']) != item['size_bytes']
                    or sha_hex(meta.get('sha256')) != sha_hex(item['sha256'])
                    or meta.get('input-fingerprint') != fingerprint
                    or meta.get('pipeline-version') != manifest.get('pipeline_version')):
                raise ValueError('objects differ from manifest')
        return {'status': 'verified', 'manifest_key': manifest_key, 'input_fingerprint': fingerprint,
                'objects': manifest['objects']}
    except (ClientError, BotoCoreError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'unverified', 'manifest_key': manifest_key,
                'detail': '완료 manifest가 없거나 현재 객체와 다릅니다. 재처리 또는 업로드 중일 수 있습니다.'}


@app.exception_handler(ClientError)
async def storage_error(request, exc):
    # Do not return exception strings, request URLs or credentials.
    from fastapi.responses import JSONResponse
    code = exc.response.get('Error', {}).get('Code', 'StorageError')
    return JSONResponse(status_code=502, content={'detail': f'R2 요청 실패 ({code}). 연결 설정과 객체 권한을 확인하세요.'})


@app.exception_handler(BotoCoreError)
async def storage_network_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=502, content={'detail': 'R2에 연결하지 못했습니다. 네트워크와 설정을 확인하세요.'})


@app.middleware('http')
async def same_origin_mutations(request: Request, call_next):
    if request.method == 'POST':
        from urllib.parse import urlsplit
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={'detail': '같은 플랫폼 화면에서 요청하세요.'})
    return await call_next(request)


def connection_key(name, settings):
    fields = ('R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_PREFIX') if name == 'R2' else (
        'HETZNER_SSH_HOST', 'HETZNER_SSH_PORT', 'HETZNER_SSH_USER', 'HETZNER_SSH_KEY_PATH', 'HETZNER_SSH_PASSWORD')
    return name, hashlib.sha256(json.dumps([settings.get(field) for field in fields]).encode()).hexdigest()


@app.get('/api/status')
def status():
    cfg = config()
    connections = []
    ready = {
        'R2': all(cfg.get(k) for k in ('R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')),
        'Hetzner SSH': all(cfg.get(k) for k in ('HETZNER_SSH_HOST', 'HETZNER_SSH_USER')),
    }
    for name, configured in ready.items():
        result = checks.get(connection_key(name, cfg), {'status': 'configured' if configured else 'missing',
                                  'detail': '설정됨 · 연결 확인 필요' if configured else 'platform/.env 설정 필요'})
        connections.append({'name': name, **result})
    return dict(connections=connections, local_observations=len(local_observations()),
                local_dongs=len(local_dongs()), gold_source='r2', gold_rows=None)


@app.post('/api/connections/check')
def check_connections():
    cfg = config()
    if all(cfg.get(k) for k in ('R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')):
        try:
            client, bucket, prefix = r2_client()
            client.list_objects_v2(Bucket=bucket, Prefix=prefix + '/', MaxKeys=1)
            checks[connection_key('R2', cfg)] = {'status': 'connected', 'detail': '버킷 목록 조회 확인'}
        except Exception:
            checks[connection_key('R2', cfg)] = {'status': 'error', 'detail': '접근 실패 · 계정·버킷·읽기 권한 확인 필요'}
    if cfg.get('HETZNER_SSH_HOST') and cfg.get('HETZNER_SSH_USER'):
        try:
            with connect_ssh(cfg):
                checks[connection_key('Hetzner SSH', cfg)] = {'status': 'connected', 'detail': 'SSH 인증 확인'}
        except Exception:
            checks[connection_key('Hetzner SSH', cfg)] = {'status': 'error', 'detail': '접속 실패 · 서버 사용자·키·known_hosts 확인 필요'}
    return status()


@app.get('/api/observations')
def observations(source: Literal['local', 'r2'] = 'local', stage: Literal['silver', 'gold'] = 'silver',
                 start: str = '', end: str = '', dong: str = '', q: str = '',
                 limit: int = Query(30, ge=1, le=100), cursor: int = Query(0, ge=0)):
    validate_dates(start, end)
    if source == 'local':
        items = [] if stage != 'silver' or (dong and dong not in local_dongs()) else [
            dict(id=opaque('local', r['observation_time_utc']), source='local', stage='silver',
                 date=r['date_kst'], observation_time=r['observation_time_kst'], boundary_version='local-seoul',
                 name=r['product_filename'], size=int(r['file_size_bytes']), kind='raster')
            for r in local_observations()]
    else:
        items = r2_index(stage)
        # Dong belongs to Parquet rows; open each date-matched object only if needed.
    items = [r for r in items if (not start or r['date'] >= start) and (not end or r['date'] <= end)
             and (not q or q.lower() in (r['name'] + r['boundary_version']).lower())]
    if source == 'r2' and dong:
        matched = []
        if len(items) > 31:
            raise HTTPException(400, 'R2 행정동 검색은 기간을 31개 관측 이하로 좁혀 주세요.')
        for item in items:
            _, key = decode(item['id'])
            parquet, _ = parquet_source(key)
            if 'dong_code' in parquet.schema_arrow.names:
                if any(dong in batch.column(0).to_pylist() for batch in parquet.iter_batches(columns=['dong_code'])):
                    matched.append(item)
        items = matched
    page = items[cursor:cursor + limit]
    return dict(items=page, total=len(items), next_cursor=cursor + limit if cursor + limit < len(items) else None)


@app.get('/api/observations/{identifier}/detail')
def detail(identifier: str, dong: str = '', swath: str = ''):
    source, key = decode(identifier)
    if source == 'local':
        record = local_record(key)
        dongs = local_dongs()
        if dong and dong not in dongs:
            raise HTTPException(404, '현재 서울 경계에 해당 행정동 코드가 없습니다.')
        meta = json.loads((ROOT / 'data/silver/gk2a/IR112_KO/processing.json').read_text())
        selection = dongs.get(dong)
        stats = {}
        if selection:
            folder = ROOT / 'data/silver/gk2a/IR112_KO'
            with rasterio.open(folder / selection['image_path']) as src, rasterio.open(folder / selection['quality_mask_path']) as mask:
                pixels = src.read(int(record['band_index']))
                valid = mask.read(int(record['band_index'])) == 1
                stats = dict(pixel_count=int(selection['geometry_pixel_count']), valid_count=int(valid.sum()),
                             dn_mean=float(pixels[valid].mean()) if valid.any() else None)
        return dict(metadata={**record, 'units': 'packed DN', 'pixel_size_m': 2000,
                              'cloud_qc_status': 'unverified', 'boundary_version': meta['boundary_version'],
                              'dong': selection['admin_dong_name'] if selection else '서울 전체'}, stats=stats, points=[],
                    preview_url=f'/api/observations/{identifier}/preview?dong={dong}', columns=[], rows=[])

    parquet, digest = parquet_source(key)
    metadata = metadata_of(parquet)
    columns = parquet.schema_arrow.names
    import pyarrow as pa
    import pyarrow.compute as pc
    rows, scenes, mapping, warnings = [], {}, {}, []
    count = valid_count = flagged_count = 0
    total = 0.0
    minimum = maximum = None
    gold_columns = {'dong_code', 'pixel_count', 'valid_count', 'temperature_mean_k',
                    'temperature_min_k', 'temperature_max_k'}
    is_gold = '/gold/' in key and gold_columns.issubset(columns)
    raw_schema = {'dong_code', 'i05_brightness_temperature_k', 'i05_quality_flags'}.issubset(columns)
    if not (is_gold or raw_schema):
        raise HTTPException(422, '지원하지 않는 Parquet 스키마입니다. Gold/Silver 컬럼을 확인하세요.')
    field = scene_column(parquet)
    # Build the scene selector from all dong-selected rows, before scene filtering.
    if field:
        summary_columns = [field, 'pixel_count' if is_gold else 'dong_code']
        if 'swath_start_utc' in columns:
            summary_columns.append('swath_start_utc')
        for batch in selected_batches(parquet, dong=dong, columns=summary_columns):
            if batch.column(field).null_count:
                raise HTTPException(422, '관측 장면 ID가 누락됐습니다.')
            aggregates = [('pixel_count', 'sum')] if is_gold else [('dong_code', 'count')]
            if 'swath_start_utc' in columns:
                aggregates.append(('swath_start_utc', 'min'))
            for row in pa.Table.from_batches([batch]).group_by(field).aggregate(aggregates).to_pylist():
                scene = str(row[field])
                item = scenes.setdefault(scene, {'swath_id': scene, 'pixel_count': 0,
                                                'swath_start_utc': row.get('swath_start_utc_min')})
                item['pixel_count'] += row.get('pixel_count_sum', row.get('dong_code_count', 0))
        if swath and swath not in scenes:
            raise HTTPException(404, '선택한 행정동에서 해당 관측 장면을 찾을 수 없습니다.')
    elif swath:
        raise HTTPException(400, '단일 장면 파일에는 장면 필터를 지정하지 마세요.')
    for batch in selected_batches(parquet, dong=dong, swath=swath):
        if len(rows) < 100:
            rows.extend([{k: finite(v) for k, v in row.items()}
                         for row in batch.slice(0, 100-len(rows)).to_pylist()])
        if is_gold:
            for row in batch.to_pylist():
                n, v = int(row['pixel_count'] or 0), int(row['valid_count'] or 0)
                mean, low, high = (finite(row[name]) for name in
                                   ('temperature_mean_k', 'temperature_min_k', 'temperature_max_k'))
                if n < 0 or v < 0 or v > n or (v and (mean is None or low is None or high is None or not low <= mean <= high)):
                    raise HTTPException(422, 'Gold 픽셀 수 또는 온도 통계가 일치하지 않습니다.')
                count += n
                valid_count += v
                flagged_count += int(row.get('flagged_count') or 0)
                if v:
                    total += float(mean) * v
                    minimum = low if minimum is None else min(minimum, low)
                    maximum = high if maximum is None else max(maximum, high)
                if field:
                    mapping[(str(row['dong_code']), str(row[field]))] = float(mean) if v else None
        else:
            count += batch.num_rows
            temp, quality = batch.column('i05_brightness_temperature_k'), batch.column('i05_quality_flags')
            valid = pc.fill_null(pc.and_(pc.is_finite(temp), pc.equal(quality, 0)), False)
            flagged_count += int(pc.sum(pc.fill_null(pc.not_equal(quality, 0), False)).as_py() or 0)
            valid_temp = pc.filter(temp, valid)
            valid_count += len(valid_temp)
            if len(valid_temp):
                total += float(pc.sum(valid_temp).as_py())
                low, high = pc.min(valid_temp).as_py(), pc.max(valid_temp).as_py()
                minimum = low if minimum is None else min(minimum, low)
                maximum = high if maximum is None else max(maximum, high)

    publication = publication_info(key)
    if publication['status'] != 'verified':
        warnings.append(publication['detail'])
    else:
        label = 'gold' if is_gold else 'silver'
        if digest != sha_hex(publication['objects'][label]['sha256']):
            raise HTTPException(409, '파일이 조회 중 교체됐습니다. 관측을 다시 여세요.')
    report = quality_report(key) if not is_gold else None
    if report and not dong and not swath:
        reported = report.get('valid_flag_zero_temperature', report.get('valid_count'))
        if report.get('pixel_count') != count or (reported is not None and reported != valid_count):
            warnings.append('품질 보고서 수치가 현재 Silver와 다릅니다. 품질 검증 완료로 사용하지 마세요.')
    points, rendered = [], None
    preview_scene = swath or (max(scenes, key=lambda scene: scenes[scene]['pixel_count']) if scenes else '')
    if is_gold:
        silver_key = metadata.get('source_silver') or metadata.get('source_object_key')
        if silver_key and '/silver/' in silver_key:
            try:
                silver, silver_digest = parquet_source(silver_key)
                expected = metadata.get('source_silver_sha256') or metadata.get('source_sha256')
                if not expected or sha_hex(expected) != silver_digest:
                    warnings.append('Gold의 원본 Silver 체크섬을 확인할 수 없어 좌표 지도를 생략했습니다.')
                else:
                    points = sample_points(silver, dong=dong, swath=swath, mapping=mapping)
                    warnings.append('지도 색은 행정동×장면별 Gold 평균값입니다. 표본 좌표는 검증된 원본 Silver에서 가져왔습니다.'
                                    if points else '연결된 Silver에서 장면별 좌표 표본을 얻지 못했습니다. Gold 통계와 원본 정보를 확인하세요.')
            except (ClientError, BotoCoreError, HTTPException, OSError, ValueError):
                warnings.append('Gold 통계는 조회됐지만 원본 Silver 지도를 만들지 못했습니다.')
    else:
        points = sample_points(parquet, dong=dong, swath=swath)
        rendered = build_preview(parquet, dong, digest, STATE / 'previews', swath=preview_scene)
        if rendered and not swath and len(scenes) > 1:
            warnings.append(f'통계는 전체 {len(scenes)}개 장면 기준입니다. 원본 영상은 관측 픽셀이 가장 많은 {preview_scene} 장면을 표시합니다.')
        if not rendered:
            warnings.append('원본 영상 범위가 너무 크거나 픽셀이 없습니다. 위치 표본과 통계를 확인하세요.')
    if len(scenes) > 1 and not swath:
        warnings.append('여러 장면에서 같은 위치를 관측한 픽셀도 각각 집계됩니다. 전체 픽셀 수는 고유 지표면 면적이 아닙니다.')
    warnings.append('I5 밝기온도와 센서 QA만 확인합니다. 구름·그림자 QC 및 강수 정답 자료 결합은 미완료입니다.')
    preview_url = (f'/api/observations/{identifier}/preview?render={rendered["render_key"]}' if rendered else None)
    return dict(
        metadata={**metadata, 'object_key': key, 'sha256': digest,
                  'units': 'K', 'valid_policy': 'finite I5 and i05_quality_flags == 0',
                  'pixel_count_denominator': 'observed pixels assigned to an administrative polygon; repeated scenes count separately',
                  'cloud_qc_status': 'unverified', 'coverage_fraction': None,
                  'coverage_status': 'not computed against complete administrative area',
                  'scan_time_accuracy': 'scan start in TAI93 seconds, not exact per-pixel exposure time',
                  'preview_point_limit': 6000, 'quality_report': report,
                  'publication': publication},
        stats=dict(pixel_count=count, valid_count=valid_count, flagged_count=flagged_count,
                   valid_fraction=valid_count / count if count else None,
                   temperature_mean_k=total / valid_count if valid_count else None,
                   temperature_min_k=minimum, temperature_max_k=maximum),
        points=points, preview_url=preview_url, preview=rendered,
        scenes=sorted(scenes.values(), key=lambda row: row['swath_id']), selected_swath=swath,
        preview_swath=preview_scene, warnings=warnings, columns=columns, rows=rows)


@app.get('/api/observations/{identifier}/preview')
def preview(identifier: str, dong: str = '', render: str = ''):
    source, key = decode(identifier)
    if source != 'local':
        if not re.fullmatch(r'[a-f0-9]{64}', render):
            raise HTTPException(400, '관측 상세에서 미리보기를 다시 불러오세요.')
        path = STATE / 'previews' / (render + '.png')
        if not path.is_file():
            raise HTTPException(404, '미리보기가 만료됐습니다. 관측 상세를 다시 여세요.')
        return Response(path.read_bytes(), media_type='image/png')
    record = local_record(key)
    dongs = local_dongs()
    if dong and dong not in dongs:
        raise HTTPException(404, '행정동을 찾을 수 없습니다.')
    selected = [dongs[dong]] if dong else list(dongs.values())
    r0, c0 = min(int(d['source_row']) for d in selected), min(int(d['source_col']) for d in selected)
    r1 = max(int(d['source_row']) + int(d['height']) for d in selected)
    c1 = max(int(d['source_col']) + int(d['width']) for d in selected)
    import warnings
    from rasterio.errors import NotGeoreferencedWarning
    with warnings.catch_warnings(), rasterio.Env(GDAL_NETCDF_BOTTOMUP='NO'):
        warnings.simplefilter('ignore', NotGeoreferencedWarning)
        with rasterio.open(ROOT / record['file_path']) as src:
            data = src.read(1, window=rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0))
    lo, hi = np.percentile(data, [2, 98])
    valid = np.zeros(data.shape, dtype=bool)
    silver = ROOT / 'data/silver/gk2a/IR112_KO'
    for entry in selected:
        with rasterio.open(silver / entry['quality_mask_path']) as mask:
            included = mask.read(int(record['band_index'])) == 1
        r, c = int(entry['source_row']) - r0, int(entry['source_col']) - c0
        valid[r:r + included.shape[0], c:c + included.shape[1]] |= included
    if valid.any():
        lo, hi = np.percentile(data[valid], [2, 98])
    pixels = np.clip((hi - data.astype(float)) / max(hi - lo, 1) * 255, 0, 255).astype('uint8')
    # Encode a scientific raster preview; no interpolation or alteration of stored data.
    with rasterio.io.MemoryFile() as memory:
        with memory.open(driver='PNG', count=2, width=pixels.shape[1], height=pixels.shape[0], dtype='uint8') as dst:
            dst.write(pixels, 1)
            dst.write(valid.astype('uint8') * 255, 2)
        png = memory.read()
    return Response(png, media_type='image/png')


@app.post('/api/gold/materialize')
def materialize():
    raise HTTPException(410, 'Gold는 Hetzner 파이프라인이 생성해 R2에 게시합니다. Gold 메뉴에서 게시된 결과를 조회하세요.')


@app.get('/api/gold')
def gold(start: str = '', end: str = '', dong: str = '', limit: int = Query(100, ge=1, le=1000),
         offset: int = Query(0, ge=0), snapshot: str = ''):
    """Page authoritative R2 rows without scanning every day to invent a total."""
    validate_dates(start, end)
    items = [item for item in r2_index('gold')
             if (not start or item['date'] >= start) and (not end or item['date'] <= end)]
    current_snapshot = hashlib.sha256(json.dumps(
        [start, end, dong, [(item['id'], item['size'], item.get('etag')) for item in items]],
        sort_keys=True).encode()).hexdigest()
    if snapshot and snapshot != current_snapshot:
        raise HTTPException(409, 'R2 데이터가 갱신됐습니다. Gold 조회를 다시 실행하세요.')
    page, seen = [], 0
    for item in items:
        _, key = decode(item['id'])
        parquet, digest = parquet_source(key, expected_etag=item.get('etag'))
        required = {'dong_code', 'pixel_count', 'valid_count', 'temperature_mean_k',
                    'temperature_min_k', 'temperature_max_k'}
        if not required.issubset(parquet.schema_arrow.names):
            raise HTTPException(422, '지원하지 않는 Gold 스키마입니다. 관측 상세에서 원본을 확인하세요.')
        publication = publication_info(key)
        for batch in selected_batches(parquet, dong=dong):
            if seen + batch.num_rows <= offset:
                seen += batch.num_rows
                continue
            skip = max(0, offset - seen)
            take = min(batch.num_rows - skip, limit + 1 - len(page))
            for row in batch.slice(skip, take).to_pylist():
                page.append({**{k: finite(v) for k, v in row.items()},
                             'date': row.get('date') or item['date'],
                             'boundary_version': row.get('boundary_version') or item['boundary_version'],
                             'swath_id': row.get('swath_id') or row.get('source_observation_id'),
                             'swath_start_utc': row.get('swath_start_utc') or row.get('observation_time_utc'),
                             'source_id': item['id'], 'object_key': key, 'source_sha256': digest,
                             'generation': item['generation'], 'publication_status': publication['status']})
            seen += batch.num_rows
            if len(page) > limit:
                return dict(items=page[:limit], total=None, has_more=True,
                            next_offset=offset + limit, snapshot=current_snapshot,
                            source='r2', observation_files=len(items))
    return dict(items=page, total=seen, has_more=False, next_offset=None,
                snapshot=current_snapshot, source='r2', observation_files=len(items))


@app.get('/api/database')
@app.get('/api/database/rows')
def database_removed():
    raise HTTPException(410, '별도 SQL 데이터베이스는 사용하지 않습니다. R2의 Silver/Gold를 직접 조회하세요.')


app.mount('/static', StaticFiles(directory=HERE / 'static'), name='assets')
app.mount('/', StaticFiles(directory=HERE / 'static', html=True), name='frontend')
