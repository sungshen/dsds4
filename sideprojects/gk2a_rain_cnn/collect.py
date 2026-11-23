"""Bounded, resumable KMA collection for station-108 next-hour CNN experiments.

AWS var=RN is parsed only with the observed/documented 14-column hourly format.
RN_HR1_MI must be -60 and RN_60M_QCM=60. Negatives remain missing. Auth keys
never enter persisted request parameters, URLs, manifests, exceptions or logs.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading
import time

import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
KST = timezone(timedelta(hours=9))
AWS = 'https://apihub.kma.go.kr/api/typ01/url/awsh.php'
GK2A = 'https://apihub.kma.go.kr/api/typ05/api/GK2A/LE1B/IR112/KO/data'
STATION_FIELDS = ['observed_at_kst','station_id','rain_1h_mm','rain_valid','rain_qc',
 'rain_window_start_kst','rain_window_end_kst','rain_start_offset_minutes','rain_60m_valid_count',
 'temperature_c','humidity_pct','pressure_hpa','wind_speed_m_s','wind_direction_deg','available_at_kst',
 'source_path','source_sha256']
SOURCE_FIELDS = ['observed_at_utc','path','sha256','size_bytes','source_endpoint']
_thread = threading.local()
_netcdf_lock = threading.Lock()
_request_lock = threading.Lock()
_last_request = 0.0


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()


def parse_time(value):
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('Use an explicit timezone, e.g. +09:00')
    if result.minute or result.second or result.microsecond: raise ValueError('Use exact hour timestamps')
    return result.astimezone(KST)


def hours(start,stop,limit):
    if not 1 <= limit <= 20000 or not start < stop: raise ValueError('Invalid range or request limit')
    count=int((stop-start).total_seconds()/3600)
    if count>limit: raise ValueError(f'{count} hours exceed --limit {limit}; choose a bounded interval or explicit higher limit')
    return [start+timedelta(hours=i) for i in range(count)]


def key_value():
    value=os.environ.get('KMA_API_KEY') or dotenv_values(ROOT/'.env').get('KMA_API_KEY')
    if not value: raise ValueError('Set KMA_API_KEY in the repository .env')
    return value


def session():
    if not hasattr(_thread,'session'): _thread.session=requests.Session()
    return _thread.session


def request(url,params,key,*,stream=False):
    global _last_request
    for attempt in range(4):
        with _request_lock:
            time.sleep(max(0.0,0.3-(time.monotonic()-_last_request)))
            _last_request=time.monotonic()
        try: response=session().get(url,params={**params,'authKey':key},timeout=(10,20),allow_redirects=False,stream=stream)
        except requests.RequestException:
            if attempt==3: raise RuntimeError('KMA network transfer failed') from None
            time.sleep(2**attempt);continue
        if response.status_code==200:return response
        status=response.status_code;response.close()
        if status in (401,403): raise PermissionError(f'KMA HTTP {status}: check this API utilization permission')
        if status not in (429,500,502,503,504) or attempt==3: raise RuntimeError(f'KMA returned HTTP {status}')
        time.sleep(2**attempt)
    raise RuntimeError('KMA request did not complete')


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    temporary.replace(path)


def write_csv(path,fields,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.csv.tmp')
    with temporary.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,lineterminator='\n');writer.writeheader();writer.writerows(rows)
    temporary.replace(path)


def parse_rain(text,stamp):
    if not text.startswith('#START7777') or '#7777END' not in text or 'RN_HR1_MI' not in text or 'RN_60M_QCM' not in text:
        raise ValueError('AWS response is not the verified hourly rain format')
    records=[line.split() for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if len(records)!=1 or len(records[0])!=14:raise ValueError('Unexpected AWS row count/column layout')
    row=records[0]
    if row[0]!=stamp.astimezone(KST).strftime('%Y%m%d%H%M') or row[1]!='108':raise ValueError('AWS time or station differs from request')
    numbers=list(map(float,row[2:]))
    if not all(math.isfinite(x) for x in numbers):raise ValueError('AWS response has nonfinite values')
    rain,offset,count=numbers[4],numbers[5],numbers[8]
    valid=rain>=0 and offset==-60 and count==60
    return {'observed_at_kst':stamp.astimezone(KST).isoformat(),'station_id':'108',
      'rain_1h_mm':rain if rain>=0 else '', 'rain_valid':'true' if valid else 'false',
      'rain_qc':'0' if valid else 'incomplete_or_unknown_interval',
      'rain_window_start_kst':(stamp-timedelta(hours=1)).astimezone(KST).isoformat(),
      'rain_window_end_kst':stamp.astimezone(KST).isoformat(),
      'rain_start_offset_minutes':offset,'rain_60m_valid_count':count,
      **dict.fromkeys(['temperature_c','humidity_pct','pressure_hpa','wind_speed_m_s','wind_direction_deg','available_at_kst'],'')}


def weather_one(stamp,output,key):
    stem=stamp.strftime('%Y%m%d%H%M');path=output/'bronze/aws'/f'{stem}.txt';meta=path.with_suffix('.json')
    params={'tm':stem,'stn':'108','var':'RN','help':'1'}
    if path.exists():
        if not meta.exists() or json.loads(meta.read_text()).get('sha256')!=sha(path):raise ValueError('Cached AWS original checksum mismatch')
        text=path.read_text()
    else:
        response=request(AWS,params,key)
        raw=response.content;response.close()
        if key.encode() in raw:raise ValueError('Provider reflected a credential; response not persisted')
        text=raw.decode('utf-8')
        record=parse_rain(text,stamp)  # Fail closed before accepting an error page as Bronze data.
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.txt.tmp');temporary.write_bytes(raw);temporary.replace(path)
        write_json(meta,{'endpoint':AWS,'request_parameters':params,'sha256':sha(path),
          'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'source_units':'mm',
          'rain_interval_policy':'RN_HR1_MI == -60 and RN_60M_QCM == 60',
          'availability_mode':'retrospective_archive; operational publication time unknown'})
    record=parse_rain(text,stamp)
    return {**record,'source_path':os.path.relpath(path,output),'source_sha256':sha(path)}


def validate_satellite(path,stamp):
    from netCDF4 import Dataset
    # The underlying NetCDF/HDF5 library is not safe for concurrent readers.
    with _netcdf_lock, Dataset(path) as source:
        if source.getncattr('satellite_name')!='GK-2A' or source.getncattr('instrument_name')!='AMI':raise ValueError('Unexpected satellite/instrument')
        band=source.variables.get('image_pixel_values')
        if band is None or band.shape!=(900,900) or str(band.dtype)!='uint16' or band.getncattr('channel_name')!='IR112':raise ValueError('Unexpected GK2A band/grid')
        if not source.getncattr('file_name').endswith(stamp.astimezone(timezone.utc).strftime('_%Y%m%d%H%M.nc')):raise ValueError('Satellite acquisition time differs from request')


def legacy_sources():
    path=ROOT/'logs/gk2a_bronze_manifest.csv'
    if not path.is_file():return {}
    with path.open(encoding='utf-8-sig') as stream:
        return {datetime.fromisoformat(row['observation_time_utc']).astimezone(KST):row for row in csv.DictReader(stream)}


def satellite_one(stamp,output,key,reuse):
    utc=stamp.astimezone(timezone.utc);stem=utc.strftime('%Y%m%d%H%M');record=reuse.get(stamp)
    if record:
        path=ROOT/record['file_path']
        if sha(path)!=record['sha256']:raise ValueError('Existing repository Bronze checksum mismatch')
        validate_satellite(path,stamp)
    else:
        path=output/'bronze/gk2a'/f'gk2a_ami_le1b_ir112_ko_{stem}.nc';meta=path.with_suffix('.json')
        if path.exists():
            if not meta.exists() or json.loads(meta.read_text()).get('sha256')!=sha(path):raise ValueError('Cached GK2A checksum mismatch')
            validate_satellite(path,stamp)
        else:
            response=request(GK2A,{'date':stem},key,stream=True)
            path.parent.mkdir(parents=True,exist_ok=True)
            with NamedTemporaryFile(dir=path.parent,suffix='.nc',delete=False) as f:
                temporary=Path(f.name)
                try:
                    for chunk in response.iter_content(1024*1024):f.write(chunk)
                except requests.RequestException:
                    temporary.unlink(missing_ok=True);raise RuntimeError('GK2A streamed transfer failed') from None
                finally:response.close()
            try:
                validate_satellite(temporary,stamp)
                temporary.replace(path)
            finally:temporary.unlink(missing_ok=True)
            write_json(meta,{'endpoint':GK2A,'request_parameters':{'date':stem},'sha256':sha(path),
              'observed_at_utc':utc.isoformat(),'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'units':'packed_DN'})
    return {'observed_at_utc':utc.isoformat(),'path':os.path.relpath(path,output),'sha256':sha(path),
            'size_bytes':path.stat().st_size,'source_endpoint':GK2A}


def collect(kind,stamps,output,*,workers=4):
    if not 1<=workers<=4:raise ValueError('Use 1–4 collection workers')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True);key=key_value();reuse=legacy_sources() if kind=='satellite' else {}
    filename='station.csv' if kind=='weather' else 'source_manifest.csv'
    fields=STATION_FIELDS if kind=='weather' else SOURCE_FIELDS
    existing={}
    if (output/filename).is_file():
        with (output/filename).open() as stream:
            for row in csv.DictReader(stream):existing[row[fields[0]]]=row
    failures=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(weather_one,s,output,key) if kind=='weather' else pool.submit(satellite_one,s,output,key,reuse):s for s in stamps}
        for future in as_completed(futures):
            stamp=futures[future]
            try:
                row=future.result();existing[row[fields[0]]]=row
                write_csv(output/filename,fields,[existing[k] for k in sorted(existing)])
            except PermissionError:
                for task in futures:task.cancel()
                raise
            except Exception as error:
                failures.append({'time_kst':stamp.isoformat(),'error_type':type(error).__name__,'detail':str(error).replace(key,'[REDACTED]')[:200]})
    report={'kind':kind,'requested':len(stamps),'stored_records':len(existing),'failures':failures,
            'manifest':str(output/filename),'collector_code_sha256':sha(__file__),
            'checked_at_utc':datetime.now(timezone.utc).isoformat()}
    write_json(output/(kind+'-collection.json'),report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['weather','satellite'])
    parser.add_argument('--start',type=parse_time,required=True);parser.add_argument('--stop',type=parse_time,required=True)
    parser.add_argument('--limit',type=int,default=72,help='Explicit request ceiling; no implicit full-period download')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--out',type=Path,default=HERE/'data/collected')
    args=parser.parse_args();result=collect(args.kind,hours(args.start,args.stop,args.limit),args.out,workers=args.workers)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if result['failures'] else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,PermissionError,RuntimeError) as error:raise SystemExit(str(error)) from None
