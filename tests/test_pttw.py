import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests

from src import pttw


def make_xlsx(path: Path, rows: list[list]):
    """Write a minimal one-sheet xlsx using inline strings."""
    def cell(ref, v):
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>' if v is not None else ""
    body = ""
    for i, row in enumerate(rows, 1):
        cells = "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(row))
        body += f'<row r="{i}">{cells}</row>'
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f"<sheetData>{body}</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def src(lat, lon, water="Ground Water", cat="Industrial", purpose="Manufacturing", litres=36500.0):
    return {"lat": lat, "lon": lon, "water": water, "category": cat, "purpose": purpose,
            "litres_year": litres, "permit": "p", "name": "n"}


def permit(lat, lon, water="Ground Water", cat="Industrial", purpose="Manufacturing", max_l=1000.0, holder="H"):
    return {"lat": lat, "lon": lon, "water": water, "category": cat, "purpose": purpose,
            "max_l_day": max_l, "holder": holder, "source": "Src01", "permit": "p"}


class GeometryTests(unittest.TestCase):
    def test_utm_matches_ministry_lat_lon(self):
        # Row from the permits spreadsheet: UTM 17, 544593 E, 4813822 N
        lat, lon = pttw.utm_to_latlon(17, 544593, 4813822)
        self.assertAlmostEqual(lat, 43.4759853462, places=5)
        self.assertAlmostEqual(lon, -80.4486279935, places=5)

    def test_distance(self):
        self.assertAlmostEqual(pttw.distance_m(45.0, -75.0, 45.01, -75.0), 1112, delta=5)
        self.assertEqual(pttw.distance_m(45.0, -75.0, 45.0, -75.0), 0)

    def test_point_in_polygon_with_hole(self):
        outer = [[0, 0], [10, 0], [10, 10], [0, 10]]
        hole = [[4, 4], [6, 4], [6, 6], [4, 6]]
        self.assertTrue(pttw.point_in_rings(2, 2, [outer, hole]))
        self.assertFalse(pttw.point_in_rings(5, 5, [outer, hole]))
        self.assertFalse(pttw.point_in_rings(20, 20, [outer, hole]))


class XlsxTests(unittest.TestCase):
    def test_reads_rows_by_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.xlsx"
            make_xlsx(path, [["A", "B"], ["1", "x"], ["2", None]])
            rows = list(pttw.read_xlsx(path, 1))
        self.assertEqual(rows, [{"A": "1", "B": "x"}, {"A": "2", "B": None}])

    def test_missing_file(self):
        with self.assertRaises(pttw.DataMissingError):
            list(pttw.read_xlsx(Path("nope.xlsx")))


class ClassificationTests(unittest.TestCase):
    def test_supply_vs_non_supply(self):
        self.assertTrue(pttw.is_supply("Industrial", "Manufacturing"))
        self.assertTrue(pttw.is_supply("Water Supply", "Municipal"))
        self.assertFalse(pttw.is_supply("Dewatering Construction", "Construction"))
        self.assertFalse(pttw.is_supply("Industrial", "Cooling Water"))
        self.assertFalse(pttw.is_supply("INDUSTRIAL", "power production"))

    def test_water_types(self):
        self.assertTrue(pttw.uses_groundwater("Ground Water"))
        self.assertTrue(pttw.uses_groundwater("Surface and Ground Water"))
        self.assertFalse(pttw.uses_groundwater("Surface Water"))
        self.assertTrue(pttw.uses_surface_water("Surface and Ground Water"))

    def test_num_handles_sentinels(self):
        self.assertIsNone(pttw._num("NR"))
        self.assertIsNone(pttw._num("NA"))
        self.assertIsNone(pttw._num(None))
        self.assertEqual(pttw._num("0"), 0.0)


class DemandTests(unittest.TestCase):
    def tables(self):
        return {"data_year": 2022, "unreported_rows": 1,
                "reported": [
                    src(45.00, -75.00),                                     # in range, supply, ground
                    src(45.00, -75.001, litres=73000.0),                    # in range
                    src(45.00, -75.00, cat="Dewatering", purpose="Pits and Quarries"),  # excluded purpose
                    src(45.00, -75.00, water="Surface Water"),              # surface: not groundwater
                    src(45.00, -75.00, litres=None),                        # not reported
                    src(45.50, -75.00),                                     # far away
                ],
                "permits": [
                    permit(45.00, -75.00, max_l=1000.0),
                    permit(45.00, -75.00, purpose="Cooling Water", max_l=999999.0),   # excluded
                    permit(45.60, -75.00, max_l=500.0),                                # far
                ]}

    def test_groundwater_demand_sums_only_supply_groundwater_in_range(self):
        out = pttw.groundwater_demand(45.0, -75.0, 2000, self.tables())
        self.assertEqual(out["reported_l_per_day"], round((36500 + 73000) / 365))   # 300
        self.assertEqual(out["sources_reporting"], 2)
        self.assertEqual(out["sources_not_reported"], 1)
        self.assertEqual(out["permitted_max_l_per_day"], 1000)
        self.assertEqual(out["active_permit_sources"], 1)

    def test_nothing_in_range(self):
        out = pttw.groundwater_demand(44.8, -75.0, 2000, self.tables())
        self.assertEqual((out["reported_l_per_day"], out["sources_reporting"]), (0, 0))

    def test_surface_context_excludes_construction_but_keeps_cooling_water(self):
        t = {"permits": [
            permit(45, -75, water="Surface Water", cat="Industrial", purpose="Cooling Water", max_l=5e6, holder="Plant"),
            permit(45, -75, water="Surface Water", cat="Dewatering Construction", purpose="Construction", max_l=9e9),
            permit(45, -75, water="Ground Water", max_l=1e9),
        ]}
        out = pttw.nearby_surface_takings(45, -75, t)
        self.assertEqual([o["holder"] for o in out], ["Plant"])
        self.assertFalse(out[0]["counted_as_supply_demand"])


class RankingTests(unittest.TestCase):
    def test_zero_demand_ranks_lowest_even_when_most_points_are_zero(self):
        pts = [0.0] * 8 + [100.0, 200.0]
        self.assertEqual(pttw.percentile_of(0.0, pts), 0.0)
        self.assertEqual(pttw.band_for(0.0), "low")
        self.assertEqual(pttw.percentile_of(150.0, pts), 90.0)
        self.assertEqual(pttw.band_for(90.0), "high")

    def test_bands(self):
        self.assertEqual(pttw.band_for(50), "moderate")
        self.assertEqual(pttw.band_for(100 / 3), "moderate")


class BoundaryAndAssessTests(unittest.TestCase):
    SQUARE = [[[[-76.0, 45.0], [-75.0, 45.0], [-75.0, 46.0], [-76.0, 46.0]]]]

    def test_outside_ottawa_raises(self):
        with mock.patch("src.pttw.load_boundary", return_value=self.SQUARE):
            with self.assertRaises(pttw.OutsideOttawaError):
                pttw.assess_site(43.65, -79.38)

    def test_assess_site_end_to_end(self):
        tables = {"data_year": 2022, "unreported_rows": 0, "permits": [],
                  "reported": [src(45.5, -75.5, litres=365000.0)]}   # 1,000 L/day
        with mock.patch("src.pttw.load_boundary", return_value=self.SQUARE), \
             mock.patch("src.pttw.load_tables", return_value=tables), \
             mock.patch("src.pttw.BBOX", {"lat": (45.0, 46.0), "lon": (-76.0, -75.0)}):
            out = pttw.assess_site(45.5, -75.5, radii_m=(2000,))
        g = out["groundwater_supply_demand"]["2000m"]
        self.assertEqual(g["reported_l_per_day"], 1000)
        self.assertEqual(g["band"], "high")
        self.assertGreater(g["city_points_with_zero"], 0)
        self.assertEqual(out["data_year"], 2022)
        self.assertTrue(out["notes"])

    def test_boundary_fetch_error(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("src.pttw.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(pttw.DataMissingError):
                pttw.load_boundary(Path(d) / "b.json")

    def test_boundary_cached(self):
        payload = {"features": [{"geometry": {"rings": self.SQUARE[0]}}]}
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        with tempfile.TemporaryDirectory() as d, mock.patch("src.pttw.requests.get", return_value=resp) as get:
            path = Path(d) / "b.json"
            self.assertEqual(pttw.load_boundary(path), self.SQUARE)
            pttw.load_boundary(path)
            self.assertEqual(get.call_count, 1)

    def test_missing_water_taking_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(pttw.DataMissingError):
                pttw.latest_water_taking_file(Path(d))


if __name__ == "__main__":
    unittest.main()
