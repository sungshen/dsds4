"""Synthetic alignment fixtures: no satellite or weather values are fabricated for real runs."""
import csv,importlib.util,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
import numpy as np
spec=importlib.util.spec_from_file_location('rain_dataset',Path(__file__).with_name('dataset.py'))
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

class DatasetTests(unittest.TestCase):
 def fixture(self,root,*,missing=False,valid=True):
  origin=datetime(2024,7,17,8,tzinfo=d.KST);rows=[]
  for offset in (3,2,1,0):
   if missing and offset==2:continue
   path=root/f'frame{offset}.npz';np.savez_compressed(path,dn=np.zeros((2,2),np.uint16),valid=np.ones((2,2),np.uint8))
   rows.append({'observed_at_utc':(origin-timedelta(hours=offset)).isoformat(),'path':path.name,'sha256':d.digest(path),'valid_pixel_count':4})
  patch=root/'patch_manifest.csv';d.write_csv(patch,rows[0].keys(),rows)
  label={'observed_at_kst':(origin+timedelta(hours=1)).isoformat(),'station_id':'108','rain_1h_mm':'32.2','rain_valid':str(valid).lower(),
         'rain_window_start_kst':origin.isoformat(),'rain_window_end_kst':(origin+timedelta(hours=1)).isoformat()}
  station=root/'station.csv';d.write_csv(station,label.keys(),[label]);return patch,station
 def test_next_hour_target_and_exact_frame_order(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);p,s=self.fixture(root);report=d.build_samples(p,s,root/'samples')
   self.assertEqual(report['sample_count'],1)
   with (root/'samples/samples.csv').open() as f:row=next(csv.DictReader(f))
   self.assertEqual(row['label'],'1');self.assertEqual(row['target_start_kst'],'2024-07-17T08:00:00+09:00')
   self.assertEqual(len(json.loads(row['frame_paths'])),4);self.assertEqual(row['temperature_c'],'')
 def test_missing_history_or_invalid_label_is_skipped(self):
  for kwargs in ({'missing':True},{'valid':False}):
   with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);p,s=self.fixture(root,**kwargs);self.assertEqual(d.build_samples(p,s,root/'samples')['sample_count'],0)
 def test_wrong_accumulation_and_corruption_are_rejected(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);p,s=self.fixture(root)
   text=s.read_text().replace('2024-07-17T08:00:00','2024-07-17T06:00:00');s.write_text(text)
   with self.assertRaisesRegex(ValueError,'one-hour'):d.build_samples(p,s,root/'samples')
   (root/'frame0.npz').write_bytes(b'bad')
   with self.assertRaisesRegex(ValueError,'checksum'):d.load_patches(p)
 def test_split_boundaries_and_publication_availability(self):
  self.assertIsNone(d.assigned_split(datetime(2025,7,1,1,tzinfo=d.KST)))
  issue=datetime(2024,7,17,8,tzinfo=d.KST)
  self.assertEqual(d.numeric_features({'temperature_c':'30'},issue)['temperature_c'],'')
  self.assertEqual(d.numeric_features({'temperature_c':'30','available_at_kst':'2024-07-17T08:05:00+09:00'},issue)['temperature_c'],'')

if __name__=='__main__':unittest.main()
