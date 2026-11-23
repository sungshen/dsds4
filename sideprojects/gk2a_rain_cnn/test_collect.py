"""Offline checks of observed AWS format, missingness and collection boundaries."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('rain_collect',Path(__file__).with_name('collect.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class CollectorTests(unittest.TestCase):
 def response(self,rain='32.2',offset='-60',count='60'):
  return f'#START7777\n# RN_HR1_MI RN_60M_QCM\n202407170900 108 43 60 45.3 0 {rain} {offset} 32.2 0 {count} 16.9 -4 60\n#7777END\n'
 def test_actual_hour_format_preserves_rain_and_interval(self):
  row=c.parse_rain(self.response(),c.parse_time('2024-07-17T09:00:00+09:00'))
  self.assertEqual(row['rain_1h_mm'],32.2);self.assertEqual(row['rain_valid'],'true')
  self.assertEqual(row['rain_window_start_kst'],'2024-07-17T08:00:00+09:00')
 def test_missing_and_partial_accumulations_do_not_become_dry(self):
  stamp=c.parse_time('2024-07-17T09:00:00+09:00')
  for args in [('-99','-60','60'),('0','-30','60'),('32.2','-60','59')]:
   self.assertEqual(c.parse_rain(self.response(*args),stamp)['rain_valid'],'false')
  self.assertEqual(c.parse_rain(self.response('-99'),stamp)['rain_1h_mm'],'')
  self.assertEqual(c.parse_rain(self.response('0'),stamp)['rain_1h_mm'],0)
 def test_wrong_time_station_and_error_pages_are_rejected(self):
  stamp=c.parse_time('2024-07-17T09:00:00+09:00')
  for text in ['<html>login</html>',self.response().replace('108','109'),self.response().replace('0900','1000')]:
   with self.assertRaises(ValueError):c.parse_rain(text,stamp)
 def test_utc_kst_and_request_ceiling(self):
  self.assertEqual(c.parse_time('2024-07-17T00:00:00Z'),c.parse_time('2024-07-17T09:00:00+09:00'))
  with self.assertRaises(ValueError):c.parse_time('2024-07-17T08:58:00+09:00')
  with self.assertRaises(ValueError):c.hours(c.parse_time('2024-07-17T00:00:00+09:00'),c.parse_time('2024-07-18T00:00:00+09:00'),4)

if __name__=='__main__':unittest.main()
