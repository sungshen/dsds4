"""Regression checks for the read-only R2 explorer; no real credentials/network."""
import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

FOLDER = Path(__file__).resolve().parent / 'platform'
sys.path.insert(0, str(FOLDER))
spec = importlib.util.spec_from_file_location('dsds_explorer_test', FOLDER / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
from raster_preview import build_preview

FAKE = {'R2_ACCOUNT_ID': '0' * 32, 'R2_BUCKET': 'test', 'R2_PREFIX': 'viirs-daily'}
BASE = 'sgis-2024-2q/2024/01/03'
SILVER = f'viirs-daily/silver/{BASE}/all-observations.parquet'
GOLD = f'viirs-daily/gold/{BASE}/dong-observations.parquet'


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, 'config', return_value=FAKE))
        self.stack.enter_context(patch.object(server, 'STATE', self.path))
        self.utc = datetime(2024, 1, 3, 3, 0, tzinfo=timezone.utc)
        self.silver = self.write('silver', pa.table({
            'dong_code': ['001', '001', '001', '002'],
            'row': pa.array([0, 0, 0, 1], type=pa.int32()),
            'column': pa.array([0, 1, 0, 0], type=pa.int32()),
            'longitude': [127.0, 127.1, 127.0, 127.2], 'latitude': [37.0, 37.1, 37.0, 37.2],
            'i05_brightness_temperature_k': [280.0, 299.0, 300.0, None],
            'i05_quality_flags': pa.array([0, 1, 0, 0], type=pa.uint16()),
            'swath_id': ['A', 'A', 'B', 'B'], 'swath_start_utc': [self.utc] * 4,
            'scan_index': [0] * 4, 'scan_start_time_tai93_s': [100.0] * 4,
        }).replace_schema_metadata({'boundary_version': 'sgis-2024-2q'}))

    def write(self, name, table):
        path = self.path / (name + '.parquet')
        pq.write_table(table, path)
        return pq.ParquetFile(path), hashlib.sha256(path.read_bytes()).hexdigest()

    def fixtures(self, mapping):
        return patch.object(server, 'parquet_source', side_effect=lambda key, **kw: mapping[key])

    def test_blank_config_does_not_erase_root_and_canonical_ssh_wins(self):
        # Call the original function, bypassing this case's config stub.
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        layers = [{'R2_BUCKET': 'root-bucket', 'HETZNER_SSH_USER': 'root',
                   'HETZNER_SSH_PASSWORD': 'server-only', 'id': 'web-user', 'pw': 'web-password'},
                  {'R2_BUCKET': '', 'HETZNER_SSH_USER': '', 'HETZNER_SSH_PASSWORD': ''}]
        with patch('dotenv.dotenv_values', side_effect=layers), patch.dict(os.environ, {}, clear=True):
            cfg = original.config()
        self.assertEqual(cfg['R2_BUCKET'], 'root-bucket')
        self.assertEqual(cfg['HETZNER_SSH_USER'], 'root')
        self.assertEqual(cfg['HETZNER_SSH_PASSWORD'], 'server-only')

    def test_multi_scene_native_preview_never_mixes_duplicate_scan_cells(self):
        with self.assertRaises(HTTPException):
            build_preview(self.silver[0], '', self.silver[1], self.path / 'preview')
        a = build_preview(self.silver[0], '', self.silver[1], self.path / 'preview', swath='A')
        b = build_preview(self.silver[0], '', self.silver[1], self.path / 'preview', swath='B')
        self.assertNotEqual(a['render_key'], b['render_key'])
        self.assertEqual((a['width'], a['height'], a['qa_excluded_count']), (2, 1, 1))
        self.assertEqual(b['missing_count'], 1)
        with rasterio.open(self.path / 'preview' / (a['render_key'] + '.png')) as image:
            self.assertEqual(image.count, 4)
            self.assertTrue((image.read(4) == 255).all())

    def test_silver_detail_scene_filters_stats_and_serializes_timestamps(self):
        with self.fixtures({SILVER: self.silver}), patch.object(server, 'quality_report', return_value=None), patch.object(server, 'publication_info', return_value={'status': 'unverified', 'detail': 'not committed'}):
            all_rows = server.detail(server.opaque('r2', SILVER))
            one_scene = server.detail(server.opaque('r2', SILVER), swath='A')
            dong = server.detail(server.opaque('r2', SILVER), dong='002')
        self.assertEqual(all_rows['stats']['pixel_count'], 4)
        self.assertEqual(all_rows['stats']['valid_count'], 2)
        self.assertEqual(all_rows['stats']['temperature_mean_k'], 290)
        self.assertEqual(len(all_rows['scenes']), 2)
        self.assertEqual(one_scene['stats']['temperature_mean_k'], 280)
        self.assertEqual(one_scene['preview']['swath_id'], 'A')
        self.assertIsNone(dong['stats']['temperature_mean_k'])
        json.dumps(jsonable_encoder(all_rows), allow_nan=False)

    def gold_file(self, name, rows):
        return self.write(name, pa.Table.from_pylist(rows).replace_schema_metadata({
            'source_silver': SILVER, 'source_silver_sha256': self.silver[1]}))

    def gold_row(self, code='001', scene='A', count=2, valid=1, mean=280.):
        return {'date': date(2024, 1, 3), 'boundary_version': 'sgis-2024-2q', 'dong_code': code,
                'swath_id': scene, 'swath_start_utc': self.utc, 'pixel_count': count,
                'valid_count': valid, 'flagged_count': count - valid,
                'temperature_mean_k': mean, 'temperature_min_k': mean,
                'temperature_max_k': mean, 'valid_fraction': valid/count if count else None}

    def test_gold_detail_uses_valid_pixel_weighted_mean(self):
        gold = self.gold_file('gold', [self.gold_row(count=2, valid=1, mean=280.),
                                      self.gold_row(scene='B', count=4, valid=3, mean=300.)])
        with self.fixtures({SILVER: self.silver, GOLD: gold}), patch.object(server, 'publication_info', return_value={'status': 'unverified', 'detail': 'not committed'}):
            result = server.detail(server.opaque('r2', GOLD))
        self.assertEqual(result['stats']['temperature_mean_k'], 295)
        self.assertEqual(result['stats']['pixel_count'], 6)
        self.assertTrue(result['points'])
        self.assertIsNone(result['preview_url'])
        json.dumps(jsonable_encoder(result), allow_nan=False)

    def test_gold_pagination_stops_early_and_has_no_sql_dependency(self):
        later = GOLD.replace('/03/', '/04/')
        first = self.gold_file('g1', [self.gold_row(code=str(i)) for i in range(3)])
        second = self.gold_file('g2', [self.gold_row(code=str(i)) for i in range(3, 5)])
        keys = [GOLD, later]
        items = [{'id': server.opaque('r2', key), 'date': f'2024-01-0{3+i}',
                  'boundary_version': 'sgis-2024-2q', 'generation': 'all-observations', 'size': 100}
                 for i, key in enumerate(keys)]
        with self.fixtures(dict(zip(keys, [first, second]))) as read, patch.object(server, 'r2_index', return_value=items), patch.object(server, 'publication_info', return_value={'status': 'unverified'}):
            one = server.gold(limit=2, offset=0)
            self.assertEqual(read.call_count, 1)
            two = server.gold(limit=2, offset=2, snapshot=one['snapshot'])
            three = server.gold(limit=2, offset=4, snapshot=one['snapshot'])
            beyond = server.gold(limit=2, offset=100, snapshot=one['snapshot'])
            with self.assertRaises(HTTPException) as error:
                server.gold(limit=2, offset=2, snapshot='changed')
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual([r['dong_code'] for r in one['items'] + two['items'] + three['items']], list('01234'))
        self.assertIsNone(one['total'])
        self.assertEqual(three['total'], 5)
        self.assertFalse(three['has_more'])
        self.assertEqual(beyond['total'], 5)

    def test_integrated_index_supersedes_only_its_own_boundary_day(self):
        class Paginator:
            def paginate(self, **kw):
                return [{'Contents': [{'Key': key, 'Size': 1} for key in [SILVER,
                    SILVER.replace('all-observations', 'A2024003.0306'),
                    SILVER.replace('sgis-2024-2q', 'other').replace('all-observations', 'A2024003.0306')]]}]
        class Client:
            def get_paginator(self, name):
                return Paginator()
        with patch.object(server, 'r2_client', return_value=(Client(), 'test', 'viirs-daily')), patch.object(server, 'index_cache', {}):
            items = server.r2_index('silver')
        self.assertEqual(len(items), 2)
        self.assertEqual(sum(r['generation'] == 'all-observations' for r in items), 1)

    def test_completion_requires_all_three_objects_current(self):
        paths = {'silver': SILVER, 'gold': GOLD,
                 'quality': SILVER.replace('/silver/', '/quality/').replace('.parquet', '.json')}
        sha = base64.b64encode(bytes(32)).decode()
        manifest = {'manifest_version': 1, 'pipeline_version': 'test', 'input_fingerprint': 'a'*64,
                    'objects': {label: {'key': key, 'size_bytes': 12, 'sha256': sha} for label, key in paths.items()}}
        class Client:
            wrong = False
            def get_object(self, **kw):
                return {'Body': io.BytesIO(json.dumps(manifest).encode())}
            def head_object(self, **kw):
                return {'ContentLength': 13 if self.wrong and kw['Key'] == GOLD else 12,
                        'Metadata': {'sha256': sha, 'input-fingerprint': 'a'*64, 'pipeline-version': 'test'}}
        client = Client()
        with patch.object(server, 'r2_client', return_value=(client, 'test', 'viirs-daily')):
            self.assertEqual(server.publication_info(SILVER)['status'], 'verified')
            client.wrong = True
            self.assertEqual(server.publication_info(SILVER)['status'], 'unverified')

    def test_invalid_identifier_and_removed_writes_are_explicit(self):
        for invalid in ('!', base64.urlsafe_b64encode(b'[]').decode(), base64.urlsafe_b64encode(b'{}').decode()):
            with self.assertRaises(HTTPException) as error:
                server.decode(invalid)
            self.assertEqual(error.exception.status_code, 400)
        for old_api in (server.materialize, server.database_removed):
            with self.assertRaises(HTTPException) as error:
                old_api()
            self.assertEqual(error.exception.status_code, 410)
        with self.assertRaises(HTTPException):
            server.validate_dates('20240101', '')


if __name__ == '__main__':
    unittest.main()
