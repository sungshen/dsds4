"""Reproduce the bounded real-observation engineering pilot, never an operational forecast."""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import collect
import dataset
import train

HERE=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name',default='pilot-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--epochs',type=int,default=20)
    args=parser.parse_args()
    if not args.run_name or Path(args.run_name).name!=args.run_name or args.run_name in ('.','..'):
        raise ValueError('Run name must be one directory name')
    output=HERE/'runs'/args.run_name
    if output.exists():raise ValueError('Run already exists; choose a new name')
    plan=json.loads((HERE/'pilot-plan.json').read_text())
    threshold=float(plan['threshold_mm'])
    raw=HERE/'data/collected'
    for day in plan['rain_label_days_kst']:
        start=collect.parse_time(day+'T00:00:00+09:00')
        weather=collect.collect('weather',collect.hours(start,start+timedelta(days=1),24),raw)
        images=collect.collect('satellite',collect.hours(start-timedelta(hours=4),start+timedelta(hours=23),27),raw)
        if weather['failures'] or images['failures']:raise RuntimeError('Incomplete collection; inspect collection reports and rerun')
    prepared=HERE/'data/prepared'
    dataset.build_patches(raw/'source_manifest.csv',prepared,patch_size=plan['patch_size'])
    definitions=json.loads((HERE/plan['split_config']).read_text())
    splits=tuple((v['name'],dataset.parse_time(v['start_kst']),dataset.parse_time(v['stop_kst_exclusive'])) for v in definitions)
    summary=dataset.build_samples(prepared/'patch_manifest.csv',raw/'station.csv',prepared,
                                  threshold=threshold,splits=splits)
    print(json.dumps({'sample_counts':summary['split_counts'],'positive_counts':summary['positive_counts']}),flush=True)
    result=train.train(SimpleNamespace(samples=prepared/'samples.csv',output=output,threshold_mm=threshold,
       min_class_samples=1,pilot_run=True,epochs=args.epochs,batch_size=16,learning_rate=1e-3,seed=42,device='cpu',with_numeric=False))
    train.write_json(output/'pilot-plan.json',plan)
    train.write_json(output/'code-lineage.json',{'sha256':{name:dataset.digest(HERE/name) for name in ('collect.py','dataset.py','train.py','run_pilot.py')},
        'native_grid_code_sha256':dataset.digest(dataset.ROOT/'crop_gk2a_silver.py')})
    print(json.dumps({'run':str(output),'synthetic':False,'purpose':'engineering pilot only','test':result['test']}))

if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,PermissionError) as error:raise SystemExit(str(error)) from None
