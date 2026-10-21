import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import uhi


def fake_response(payload, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def pixel(value):
    return {"results": [{"attributes": {"Pixel Value": value}}]}


class IdentifyTests(unittest.TestCase):
    @mock.patch("src.uhi.requests.get")
    def test_valid_value(self, get):
        get.return_value = fake_response(pixel("27.957371"))
        self.assertAlmostEqual(uhi.identify_pixel(-8430000, 5680000), 27.957371)

    @mock.patch("src.uhi.requests.get")
    def test_nodata_and_empty_results_are_none(self, get):
        get.return_value = fake_response(pixel("NoData"))
        self.assertIsNone(uhi.identify_pixel(0, 0))
        get.return_value = fake_response({"results": []})
        self.assertIsNone(uhi.identify_pixel(0, 0))

    @mock.patch("src.uhi.requests.get")
    def test_http_error_raises_service_error(self, get):
        get.return_value = fake_response({}, status=503)
        with self.assertRaises(uhi.ServiceError):
            uhi.identify_pixel(0, 0)

    @mock.patch("src.uhi.requests.get", side_effect=requests.ConnectionError("down"))
    def test_connection_error(self, _):
        with self.assertRaises(uhi.ServiceError):
            uhi.identify_pixel(0, 0)

    @mock.patch("src.uhi.requests.get")
    def test_arcgis_error_body(self, get):
        get.return_value = fake_response({"error": {"code": 400, "message": "bad"}})
        with self.assertRaisesRegex(uhi.ServiceError, "400"):
            uhi.identify_pixel(0, 0)


class BufferTests(unittest.TestCase):
    def test_buffer_points_inside_circle_and_mercator_corrected(self):
        pts = uhi.buffer_points(0, 0, 500, 5, lat=45.35)
        self.assertEqual(len(pts), 13)  # 5x5 lattice, points with dx^2+dy^2 <= r^2
        r = 500 / 0.70  # ~ stretched radius
        self.assertTrue(all(x * x + y * y <= (r * 1.01) ** 2 for x, y in pts))

    @mock.patch("src.uhi.identify_pixel")
    def test_mean_ignores_nodata(self, ident):
        n = len(uhi.buffer_points(*uhi.to_web_mercator(45.35, -75.91), 500, 5, 45.35))
        ident.side_effect = [30.0, 32.0] + [None] * (n - 2)
        mean, valid, total = uhi.buffer_mean(45.35, -75.91)
        self.assertEqual((mean, valid, total), (31.0, 2, n))

    @mock.patch("src.uhi.identify_pixel", return_value=None)
    def test_all_nodata_raises(self, _):
        with self.assertRaises(uhi.NoDataError):
            uhi.buffer_mean(45.35, -75.91)

    def test_outside_extent_raises_without_network(self):
        with mock.patch("src.uhi.identify_pixel") as ident:
            with self.assertRaises(uhi.OutOfExtentError):
                uhi.buffer_mean(43.65, -79.38)  # Toronto
            ident.assert_not_called()


class ScoringTests(unittest.TestCase):
    def test_percentile_and_bands(self):
        dist = list(range(1, 101))
        self.assertAlmostEqual(uhi.percentile_of(50, dist), 49.5, places=1)
        self.assertEqual(uhi.band_for(10), "low")
        self.assertEqual(uhi.band_for(50), "moderate")
        self.assertEqual(uhi.band_for(90), "high")
        self.assertEqual(uhi.band_for(100 / 3), "moderate")  # boundary goes up



class CacheTests(unittest.TestCase):
    def test_cache_written_then_reused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "grid.json"
            with mock.patch("src.uhi.sample_city_grid", return_value=[20.0, 25.0, 30.0]) as s:
                self.assertEqual(uhi.load_city_distribution(path), [20.0, 25.0, 30.0])
                self.assertEqual(uhi.load_city_distribution(path), [20.0, 25.0, 30.0])
                self.assertEqual(s.call_count, 1)
            self.assertEqual(json.loads(path.read_text())["grid_size"], uhi.CITY_GRID_SIZE)

class AssessmentTests(unittest.TestCase):
    DIST = tuple(float(v) for v in range(20, 40))   # city median 29.5

    def _run(self, means_by_layer):
        def fake_mean(lat, lon, radius_m, grid_size, session=None, layer_id=2):
            m = means_by_layer[layer_id]
            if isinstance(m, Exception):
                raise m
            return m, 13, 13
        with mock.patch("src.uhi.buffer_mean", side_effect=fake_mean), \
             mock.patch("src.uhi.load_city_distribution", return_value=list(self.DIST)):
            return uhi.assess_site(45.35, -75.91)

    def test_single_year_numbers(self):
        with mock.patch("src.uhi.buffer_mean", return_value=(38.5, 13, 13)), \
             mock.patch("src.uhi.load_city_distribution", return_value=list(self.DIST)):
            out = uhi.year_assessment(45.35, -75.91)
        self.assertEqual(out["year"], 2023)
        self.assertEqual(out["above_city_median_c"], 9.0)
        self.assertEqual(out["city_median_c"], 29.5)
        self.assertEqual(out["band"], "high")

    def test_persistent_hotspot(self):
        out = self._run({0: 38.0, 1: 38.5, 2: 39.0})
        self.assertEqual(out["assessment"], "persistent_hotspot")
        self.assertEqual(out["years_in_top_third"], "3/3")

    def test_single_hot_year_is_intermittent(self):
        out = self._run({0: 21.0, 1: 21.0, 2: 39.0})
        self.assertEqual(out["assessment"], "intermittent")
        self.assertEqual(out["years_in_top_third"], "1/3")

    def test_never_hot(self):
        self.assertEqual(self._run({0: 22.0, 1: 23.0, 2: 25.0})["assessment"], "not_hotspot")

    def test_skipped_year_is_reported(self):
        out = self._run({0: uhi.NoDataError("water"), 1: 38.0, 2: 38.0})
        self.assertEqual(sorted(out["years"]), [2021, 2023])
        self.assertIn(2019, out["skipped"])
        self.assertEqual(out["assessment"], "persistent_hotspot")

    def test_one_available_year_cannot_be_persistent(self):
        err = uhi.NoDataError("x")
        self.assertEqual(self._run({0: err, 1: err, 2: 39.0})["assessment"], "intermittent")

    def test_all_years_failing_raises(self):
        err = uhi.NoDataError("x")
        with self.assertRaises(uhi.NoDataError):
            self._run({0: err, 1: err, 2: err})

    def test_cache_is_per_layer(self):
        self.assertNotEqual(uhi.cache_path_for(0), uhi.cache_path_for(2))


if __name__ == "__main__":
    unittest.main()
