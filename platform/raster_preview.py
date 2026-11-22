"""Render native VIIRS scan cells without interpolation; retain selection and QA masks."""
import hashlib
import json
import os
import tempfile
import warnings
from pathlib import Path

import numpy as np
import rasterio
from fastapi import HTTPException
from rasterio.errors import NotGeoreferencedWarning


def build_preview(parquet, dong, digest, folder, *, swath=''):
    token = hashlib.sha256(f'native-scan-v2:{digest}:{dong}:{swath}'.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / (token + '.png')
    metadata_path = folder / (token + '.json')
    if image_path.exists() and metadata_path.exists():
        try:
            return json.loads(metadata_path.read_text())
        except (ValueError, OSError):
            pass
    columns = ['row', 'column', 'dong_code', 'i05_brightness_temperature_k', 'i05_quality_flags']
    if not set(columns).issubset(parquet.schema_arrow.names):
        return None
    has_scenes = 'swath_id' in parquet.schema_arrow.names
    if has_scenes:
        if not swath:
            raise HTTPException(400, '여러 관측의 원본 픽셀 영상은 장면을 선택해야 합니다.')
        columns.append('swath_id')
    chunks = []
    for batch in parquet.iter_batches(columns=columns, batch_size=65536):
        selected = np.ones(batch.num_rows, dtype=bool)
        if dong:
            selected = np.asarray(batch.column(2).to_pylist()) == dong
        if has_scenes:
            selected &= np.asarray(batch.column(5).to_pylist()) == swath
        if selected.any():
            chunks.append(tuple(batch.column(i).to_numpy(zero_copy_only=False)[selected]
                                for i in (0, 1, 3, 4)))
    if not chunks:
        return None
    rows, cols, temperatures, flags = [np.concatenate([chunk[i] for chunk in chunks]) for i in range(4)]
    if (not np.isfinite(rows).all() or not np.isfinite(cols).all()
            or (rows < 0).any() or (cols < 0).any()
            or not (rows == np.floor(rows)).all() or not (cols == np.floor(cols)).all()):
        raise HTTPException(422, '원본 픽셀 행·열이 올바르지 않습니다.')
    height, width = int(rows.max() - rows.min() + 1), int(cols.max() - cols.min() + 1)
    if height * width > 16_000_000:
        return None
    row_origin, column_origin = int(rows.min()), int(cols.min())
    rows = rows.astype('int64') - row_origin
    cols = cols.astype('int64') - column_origin
    if np.unique(rows * width + cols).size != len(rows):
        raise HTTPException(422, '원본 스캔 행·열에 중복 픽셀이 있습니다.')
    valid = np.isfinite(temperatures) & (flags == 0)
    gray = np.full(len(rows), 140, dtype='uint8')
    low, high = None, None
    if valid.any():
        low, high = float(temperatures[valid].min()), float(temperatures[valid].max())
        gray[valid] = np.clip((high - temperatures[valid]) / max(high - low, 1e-6) * 255, 0, 255).astype('uint8')
    image = np.zeros((4, height, width), dtype='uint8')
    image[:3, rows, cols] = gray
    image[:3, rows[~valid], cols[~valid]] = np.array([200, 210, 223], dtype='uint8')[:, None]
    image[3, rows, cols] = 255
    metadata = dict(kind='native_scan', width=width, height=height, render_key=token,
                    swath_id=swath or None, row_origin=row_origin, column_origin=column_origin,
                    georeferenced=False, resampling='none',
                    temperature_min_k=low, temperature_max_k=high,
                    qa_excluded_count=int((np.isfinite(temperatures) & (flags != 0)).sum()),
                    missing_count=int((~np.isfinite(temperatures)).sum()))
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        temporary = Path(temporary)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', NotGeoreferencedWarning)
            with rasterio.open(temporary / 'image.png', 'w', driver='PNG', count=4,
                               width=width, height=height, dtype='uint8') as dst:
                dst.write(image)
        (temporary / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
        os.replace(temporary / 'image.png', image_path)
        os.replace(temporary / 'metadata.json', metadata_path)
    return metadata
