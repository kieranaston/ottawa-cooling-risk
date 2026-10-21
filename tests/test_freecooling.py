import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import freecooling as fc


def epw_text(temp_for_hour, rh=50, pressure=101325, n=fc.EPW_HOURS):
    """Synthetic EPW: 8 header lines then n hourly rows (cols: ...,DB,dew,RH,pressure)."""
    lines = ["HEADER"] * fc.EPW_HEADER_LINES
    for i in range(n):
        month, day, hour = 1 + (i // 24) // 31 % 12, 1 + (i // 24) % 31, i % 24 + 1
        lines.append(f"2011,{month},{day},{hour},0,flags,{temp_for_hour(i)},0,{rh},{pressure}")
    return "\n".join(lines)


class PsychrometricTests(unittest.TestCase):
    def test_known_wet_bulb_values(self):
        # Standard psychrometric chart values at sea-level pressure
        self.assertAlmostEqual(fc.wet_bulb_c(20, 50, 101325), 13.8, delta=0.2)
        self.assertAlmostEqual(fc.wet_bulb_c(30, 50, 101325), 22.0, delta=0.2)
        self.assertAlmostEqual(fc.wet_bulb_c(25, 60, 101325), 19.5, delta=0.2)

    def test_saturated_air_wet_bulb_equals_dry_bulb(self):
        for t in (-10, 0, 15, 30):
            self.assertAlmostEqual(fc.wet_bulb_c(t, 100, 101325), t, delta=0.05)

    def test_wet_bulb_never_exceeds_dry_bulb_and_drops_with_dryness(self):
        self.assertLess(fc.wet_bulb_c(28, 30, 100000), fc.wet_bulb_c(28, 70, 100000))
        self.assertLessEqual(fc.wet_bulb_c(28, 70, 100000), 28)

    def test_saturation_pressure_reference(self):
        self.assertAlmostEqual(fc.saturation_pressure_pa(20), 2339, delta=5)
        self.assertAlmostEqual(fc.saturation_pressure_pa(0), 611, delta=3)


class SummarizeTests(unittest.TestCase):
    def test_counts_hours_and_full_days(self):
        # two days: day 1 all 10C; day 2 has one hot hour (25C), rest 10C
        hours = [(1, 1, 10.0, 5.0)] * 24 + [(1, 2, 10.0, 5.0)] * 23 + [(1, 2, 25.0, 20.0)]
        out = fc.summarize(hours, cutoffs_c=(18, 24), wb_cutoffs_c=(10,), stress_c=(20,))
        dry = out["free_cooling_dry_bulb"]
        self.assertEqual(dry["18C"]["hours"], 47)
        self.assertEqual(dry["18C"]["full_days"], 1)
        self.assertEqual(dry["24C"]["full_days"], 1)   # 25 > 24
        self.assertEqual(dry["18C"]["total_days"], 2)
        self.assertEqual(out["free_cooling_wet_bulb"]["10C"]["hours"], 47)
        self.assertEqual(out["stress_hours_dry_bulb"]["above_20C"], 1)
        self.assertEqual(out["dry_bulb_c"]["annual"], {"min": 10.0, "mean": 10.3, "max": 25.0})

    def test_cutoff_is_inclusive(self):
        out = fc.summarize([(1, 1, 18.0, 10.0)] * 24, cutoffs_c=(18,), wb_cutoffs_c=(), stress_c=())
        self.assertEqual(out["free_cooling_dry_bulb"]["18C"]["hours"], 24)



class ReadEpwTests(unittest.TestCase):
    def test_parses_dry_and_wet_bulb(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.epw"
            path.write_text(epw_text(lambda i: 20.0))
            hours = fc.read_epw(path)
        self.assertEqual(len(hours), fc.EPW_HOURS)
        self.assertEqual(hours[0][:3], (1, 1, 20.0))
        self.assertAlmostEqual(hours[0][3], 13.8, delta=0.2)

    def test_wrong_length_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.epw"
            path.write_text(epw_text(lambda i: 1.0, n=100))
            with self.assertRaises(fc.WeatherDataError):
                fc.read_epw(path)

    def test_missing_humidity_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.epw"
            path.write_text(epw_text(lambda i: 1.0, rh=999))
            with self.assertRaises(fc.WeatherDataError):
                fc.read_epw(path)

    def test_slightly_supersaturated_rh_is_clamped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.epw"
            path.write_text(epw_text(lambda i: 5.0, rh=103))
            hours = fc.read_epw(path)
        self.assertAlmostEqual(hours[0][3], 5.0, delta=0.05)  # saturated: wet-bulb == dry-bulb



class DownloadTests(unittest.TestCase):
    def _resp(self, content):
        r = mock.Mock(content=content)
        r.raise_for_status.return_value = None
        return r

    def test_download_verified_and_cached(self):
        body = b"abc"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("src.freecooling.requests.get", return_value=self._resp(body)) as get:
            path = Path(d) / "sub" / "f.epw"
            sha = hashlib.sha256(body).hexdigest()
            fc.ensure_epw(path, "http://x", sha)
            fc.ensure_epw(path, "http://x", sha)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(path.read_bytes(), body)

    def test_checksum_mismatch_refuses(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("src.freecooling.requests.get", return_value=self._resp(b"abc")):
            path = Path(d) / "f.epw"
            with self.assertRaises(fc.WeatherDataError):
                fc.ensure_epw(path, "http://x", "0" * 64)
            self.assertFalse(path.exists())

    def test_http_error(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("src.freecooling.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(fc.WeatherDataError):
                fc.ensure_epw(Path(d) / "f.epw", "http://x", None)


if __name__ == "__main__":
    unittest.main()

