import math
import unittest
from unittest import mock

import requests

from src import zoning

LAT0, LON0 = 45.35, -75.91


def offset_ll(east_m, north_m, lat0=LAT0, lon0=LON0):
    """lat/lon of a point east/north of the site (simple spherical approximation)."""
    return (lat0 + north_m / 111_132.0,
            lon0 + east_m / (111_320.0 * math.cos(math.radians(lat0))))


def rect(e1, n1, e2, n2):
    """Closed Web Mercator ring for a rectangle given in metres east/north of the site."""
    corners = [(e1, n1), (e2, n1), (e2, n2), (e1, n2), (e1, n1)]
    return [list(zoning.to_web_mercator(*offset_ll(e, n))) for e, n in corners]


def feature(rings, code="R1A", family="R1", oid=1):
    return {"attributes": {"OBJECTID": oid, "ZONE_CODE": code, "PARENTZONE": family},
            "geometry": {"rings": rings}}


def resp(payload, status=200):
    r = mock.Mock()
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        r.raise_for_status.return_value = None
    return r


def service(features, ids=None):
    """post() side effect: IDs query returns ids, by-ID queries return the matching features."""
    by_id = {f["attributes"]["OBJECTID"]: f for f in features}
    ids = list(by_id) if ids is None else ids

    def post(url, data=None, timeout=None):
        if data.get("returnIdsOnly") == "true":
            return resp({"objectIds": ids})
        if data.get("returnCountOnly") == "true":
            return resp({"count": 5})
        wanted = [int(i) for i in data["objectIds"].split(",")]
        return resp({"features": [by_id[i] for i in wanted if i in by_id]})
    return post


class ProjectionTests(unittest.TestCase):
    def test_mercator_round_trip(self):
        lat, lon = zoning.from_web_mercator(*zoning.to_web_mercator(LAT0, LON0))
        self.assertAlmostEqual(lat, LAT0, places=9)
        self.assertAlmostEqual(lon, LON0, places=9)

    def test_local_plane_distances(self):
        x, y = zoning.local_xy(LAT0, LON0, *offset_ll(300, 400))
        self.assertAlmostEqual(math.hypot(x, y), 500, delta=1.5)
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)

    def test_mercator_scale_is_not_used_for_distance(self):
        # 100 m due north is ~142 Web Mercator "metres" at this latitude; distance must be ~100.
        d = zoning.polygon_distance_m(LAT0, LON0, [rect(-50, 100, 50, 200)])
        self.assertAlmostEqual(d, 100, delta=1.0)

    def test_segment_distance(self):
        self.assertAlmostEqual(zoning.point_segment_distance(3, 4, 3, 4), 5.0)       # degenerate
        self.assertAlmostEqual(zoning.point_segment_distance(-10, 5, 10, 5), 5.0)    # perpendicular
        self.assertAlmostEqual(zoning.point_segment_distance(10, 0, 20, 0), 10.0)    # off the end


class PolygonDistanceTests(unittest.TestCase):
    def test_inside_polygon_is_zero(self):
        self.assertEqual(zoning.polygon_distance_m(LAT0, LON0, [rect(-50, -50, 50, 50)]), 0.0)

    def test_inside_hole_measures_to_hole_edge(self):
        outer, hole = rect(-500, -500, 500, 500), rect(-40, -40, 40, 40)
        self.assertAlmostEqual(zoning.polygon_distance_m(LAT0, LON0, [outer, hole]), 40, delta=1.0)

    def test_distance_to_nearest_edge_east(self):
        self.assertAlmostEqual(zoning.polygon_distance_m(LAT0, LON0, [rect(250, -100, 400, 100)]),
                               250, delta=1.0)


class FlagTests(unittest.TestCase):
    def test_thresholds_inclusive(self):
        self.assertEqual(zoning.flag_for(None, 70, 300), zoning.FLAG_CLEAR)
        self.assertEqual(zoning.flag_for(0, 70, 300), zoning.FLAG_NEAR)
        self.assertEqual(zoning.flag_for(70, 70, 300), zoning.FLAG_NEAR)
        self.assertEqual(zoning.flag_for(70.1, 70, 300), zoning.FLAG_CAUTION)
        self.assertEqual(zoning.flag_for(300, 70, 300), zoning.FLAG_CAUTION)
        self.assertEqual(zoning.flag_for(300.1, 70, 300), zoning.FLAG_CLEAR)

    def test_d6_distances(self):
        self.assertEqual(zoning.thresholds_for("I"), (20.0, 70.0))
        self.assertEqual(zoning.thresholds_for("ii"), (70.0, 300.0))
        self.assertEqual(zoning.thresholds_for("III"), (300.0, 1000.0))
        with self.assertRaises(ValueError):
            zoning.thresholds_for("IV")

    def test_default_class_is_the_most_conservative(self):
        self.assertEqual(zoning.DEFAULT_FACILITY_CLASS, "III")

    def test_every_flag_has_one_statement(self):
        self.assertEqual(set(zoning.FLAG_STATEMENTS),
                         {zoning.FLAG_NEAR, zoning.FLAG_CAUTION, zoning.FLAG_CLEAR})
        self.assertTrue(all(zoning.FLAG_STATEMENTS.values()))

    def test_statement_uses_the_class_distances(self):
        self.assertIn("300 m", zoning.statement_for(zoning.FLAG_NEAR))                       # class III
        self.assertIn("1000 m", zoning.statement_for(zoning.FLAG_CAUTION))
        self.assertIn("Class II", zoning.statement_for(zoning.FLAG_CAUTION, "II"))
        self.assertIn("70 m", zoning.statement_for(zoning.FLAG_NEAR, "II"))
        self.assertIn("300 m", zoning.statement_for(zoning.FLAG_CLEAR, "II"))

    def test_zones_conservative_and_grouped(self):
        self.assertEqual(set(zoning.RESIDENTIAL_ZONES),
                         {"R1", "R2", "R3", "R4", "R5", "RM", "RR", "V1", "V2", "V3", "RU"})
        for excluded in ("AM", "TM", "MC", "I1", "AG"):
            self.assertNotIn(excluded, zoning.RESIDENTIAL_ZONES)
        self.assertEqual(zoning.zone_group("R3"), "urban residential")
        self.assertEqual(zoning.zone_group("V2"), "village / rural residential")
        self.assertEqual(zoning.zone_group("RU"), "rural countryside")
        self.assertIsNone(zoning.zone_group("AG"))

    def test_where_clause(self):
        self.assertEqual(zoning.where_clause(["R1", "RU"]), "PARENTZONE IN ('R1','RU')")
        for bad in (["R1'; DROP"], [], ["r1"]):
            with self.assertRaises(ValueError):
                zoning.where_clause(bad)


class NearestResidentialTests(unittest.TestCase):
    def run_site(self, features, **kw):
        with mock.patch("src.zoning.requests.post", side_effect=service(features)):
            return zoning.nearest_residential(LAT0, LON0, **kw)

    def test_picks_nearest_polygon_and_reports_code_and_group(self):
        out = self.run_site([feature([rect(500, -50, 600, 50)], "R3Z", "R3", 1),
                             feature([rect(150, -50, 250, 50)], "R1Y[746]", "R1", 2)], facility_class="II")
        self.assertAlmostEqual(out["distance_m"], 150, delta=1.5)
        self.assertEqual(out["nearest_zone_code"], "R1Y[746]")
        self.assertEqual(out["nearest_zone_family"], "R1")
        self.assertEqual(out["nearest_zone_group"], "urban residential")
        self.assertEqual(out["flag"], zoning.FLAG_CAUTION)           # 70 < 150 <= 300
        self.assertFalse(out["within_min_separation"])
        self.assertTrue(out["within_influence_area"])
        self.assertEqual(out["residential_polygons_found"], 2)
        self.assertNotIn("note", out)

    def test_same_site_flags_differently_by_class(self):
        feats = [feature([rect(150, -50, 250, 50)])]
        self.assertEqual(self.run_site(feats, facility_class="I")["flag"], zoning.FLAG_CLEAR)        # > 70
        self.assertEqual(self.run_site(feats, facility_class="II")["flag"], zoning.FLAG_CAUTION)
        self.assertEqual(self.run_site(feats, facility_class="III")["flag"], zoning.FLAG_NEAR)       # <= 300

    def test_default_class_iii_and_radius_equals_influence_area(self):
        out = self.run_site([feature([rect(150, -50, 250, 50)])])
        self.assertEqual(out["facility_class"], "III")
        self.assertEqual(out["minimum_separation_m"], 300.0)
        self.assertEqual(out["influence_area_m"], 1000.0)
        self.assertEqual(out["search_radius_m"], 1000.0)
        self.assertEqual(out["flag"], zoning.FLAG_NEAR)
        self.assertEqual(out["statement"], zoning.statement_for(zoning.FLAG_NEAR, "III"))

    def test_radius_smaller_than_influence_area_is_rejected(self):
        with mock.patch("src.zoning.requests.post") as post:
            with self.assertRaises(ValueError):
                zoning.nearest_residential(LAT0, LON0, "III", radius_m=500)
            post.assert_not_called()

    def test_rural_countryside_hit_is_counted_with_a_note(self):
        out = self.run_site([feature([rect(60, -10, 120, 10)], "RU", "RU")], facility_class="II")
        self.assertEqual(out["flag"], zoning.FLAG_NEAR)
        self.assertEqual(out["nearest_zone_group"], "rural countryside")
        self.assertIn("RU", out["note"])

    def test_site_inside_residential_zone(self):
        out = self.run_site([feature([rect(-30, -30, 30, 30)])], facility_class="I")
        self.assertEqual(out["distance_m"], 0.0)
        self.assertEqual(out["flag"], zoning.FLAG_NEAR)

    def test_none_within_radius_when_area_is_mapped(self):
        out = self.run_site([], facility_class="II")
        self.assertIsNone(out["distance_m"])
        self.assertIsNone(out["nearest_zone_code"])
        self.assertFalse(out["within_min_separation"])
        self.assertFalse(out["within_influence_area"])
        self.assertEqual(out["flag"], zoning.FLAG_CLEAR)
        self.assertEqual(out["statement"], zoning.statement_for(zoning.FLAG_CLEAR, "II"))
        self.assertIn("none within radius", out["message"])

    def test_no_zoning_coverage_raises(self):
        def post(url, data=None, timeout=None):
            if data.get("returnIdsOnly") == "true":
                return resp({"objectIds": None})
            return resp({"count": 0})
        with mock.patch("src.zoning.requests.post", side_effect=post):
            with self.assertRaises(zoning.NoZoningDataError):
                zoning.nearest_residential(LAT0, LON0)

    def test_outside_extent_raises_without_network(self):
        with mock.patch("src.zoning.requests.post") as post:
            with self.assertRaises(zoning.OutOfExtentError):
                zoning.nearest_residential(43.65, -79.38)   # Toronto
            post.assert_not_called()

    def test_distance_overrides(self):
        out = self.run_site([feature([rect(150, -50, 250, 50)])], near_m=200.0, caution_m=500.0)
        self.assertTrue(out["within_min_separation"])
        self.assertEqual(out["minimum_separation_m"], 200.0)
        self.assertEqual(out["search_radius_m"], 500.0)

    def test_unknown_class_rejected(self):
        with self.assertRaises(ValueError):
            zoning.nearest_residential(LAT0, LON0, "IV")


class QueryTests(unittest.TestCase):
    def test_request_parameters(self):
        with mock.patch("src.zoning.requests.post", side_effect=service([feature([rect(100, 0, 200, 50)])])) as post:
            zoning.fetch_zone_polygons(LAT0, LON0, 1000, ["R1", "RU"])
        ids_call, geom_call = post.call_args_list[0].kwargs["data"], post.call_args_list[1].kwargs["data"]
        self.assertEqual(ids_call["where"], "PARENTZONE IN ('R1','RU')")
        self.assertEqual(ids_call["inSR"], 4326)
        self.assertEqual(ids_call["distance"], 1000)
        self.assertEqual(ids_call["geometry"], f"{LON0},{LAT0}")
        self.assertEqual(ids_call["returnIdsOnly"], "true")
        self.assertEqual(geom_call["outSR"], 3857)
        self.assertEqual(geom_call["returnGeometry"], "true")

    def test_many_polygons_fetched_in_chunks_without_duplicates(self):
        feats = [feature([rect(100 + i, 0, 101 + i, 1)], oid=i + 1) for i in range(450)]
        with mock.patch("src.zoning.requests.post", side_effect=service(feats)) as post:
            polys = zoning.fetch_zone_polygons(LAT0, LON0, 1000, ["R1"])
        self.assertEqual(len(polys), 450)
        self.assertEqual(post.call_count, 1 + 3)           # IDs + ceil(450 / 200) chunks

    def test_short_chunk_is_split_and_completed(self):
        feats = [feature([rect(100, 0, 101, 1)], oid=i + 1) for i in range(6)]
        by_id = {f["attributes"]["OBJECTID"]: f for f in feats}

        def post(url, data=None, timeout=None):
            if data.get("returnIdsOnly") == "true":
                return resp({"objectIds": list(by_id)})
            wanted = [int(i) for i in data["objectIds"].split(",")]
            return resp({"features": [by_id[i] for i in wanted][:3]})   # service truncates to 3
        with mock.patch("src.zoning.requests.post", side_effect=post):
            self.assertEqual(len(zoning.fetch_zone_polygons(LAT0, LON0, 1000, ["R1"])), 6)

    def test_missing_geometry_raises(self):
        def post(url, data=None, timeout=None):
            if data.get("returnIdsOnly") == "true":
                return resp({"objectIds": [1]})
            return resp({"features": []})
        with mock.patch("src.zoning.requests.post", side_effect=post):
            with self.assertRaises(zoning.ServiceError):
                zoning.fetch_zone_polygons(LAT0, LON0, 1000, ["R1"])


class ErrorTests(unittest.TestCase):
    def test_http_error(self):
        with mock.patch("src.zoning.requests.post", return_value=resp({}, status=503)):
            with self.assertRaises(zoning.ServiceError):
                zoning.nearest_residential(LAT0, LON0)

    def test_connection_error(self):
        with mock.patch("src.zoning.requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(zoning.ServiceError):
                zoning.nearest_residential(LAT0, LON0)

    def test_arcgis_error_body(self):
        with mock.patch("src.zoning.requests.post", return_value=resp({"error": {"code": 400, "message": "bad"}})):
            with self.assertRaisesRegex(zoning.ServiceError, "400"):
                zoning.nearest_residential(LAT0, LON0)

    def test_non_json(self):
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.side_effect = ValueError("not json")
        with mock.patch("src.zoning.requests.post", return_value=r):
            with self.assertRaises(zoning.ServiceError):
                zoning.nearest_residential(LAT0, LON0)

    def test_invalid_coordinates(self):
        with self.assertRaises(ValueError):
            zoning.to_web_mercator(95, 0)


if __name__ == "__main__":
    unittest.main()
