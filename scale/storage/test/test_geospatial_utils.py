from __future__ import unicode_literals

import django
from django.test import TestCase
from mock import mock_open, patch

import storage.geospatial_utils as geo_utils
from job.results.exceptions import InvalidResultsManifest

FEATURE_COLLECTION_GEOJSON = '{"type": "FeatureCollection", "features": [{ "type": "Feature", "properties": { "prop_a": "A", "prop_b": "B" }, "geometry": { "type": "Polygon", "coordinates": [ [ [ 1.0, 10.5 ], [ 1.1, 21.1 ], [ 1.2, 21.2 ], [ 1.3, 21.6 ], [ 1.0, 10.5 ] ] ] } }]}'
FEATURE_GEOJSON = '{"type": "Feature", "properties": { "prop_a": "A", "prop_b": "B" }, "geometry": { "type": "Polygon", "coordinates": [ [ [ 1.0, 10.5 ], [ 1.1, 21.1 ], [ 1.2, 21.2 ], [ 1.3, 21.6 ], [ 1.0, 10.5 ] ] ] } }'
FEATURE_GEOJSON_NO_PROPS = '{"type": "Feature", "geometry": { "type": "Polygon", "coordinates": [ [ [ 1.0, 10.5 ], [ 1.1, 21.1 ], [ 1.2, 21.2 ], [ 1.3, 21.6 ], [ 1.0, 10.5 ] ] ] } }'
POLYGON_GEOJSON = '{"type": "Polygon", "coordinates": [ [ [ 1.0, 10.5 ], [ 1.1, 21.1 ], [ 1.2, 21.2 ], [ 1.3, 21.6 ], [ 1.0, 10.5 ] ] ] }'


class TestGeospatialUtils(TestCase):

    def setUp(self):
        django.setup()

    @patch('__builtin__.open', mock_open(read_data=FEATURE_COLLECTION_GEOJSON), create=True)
    def test_valid_feature_collection(self):
        """Tests parsing geojson"""

        # Call method to test
        geom, props = geo_utils.parse_geo_json_file('fake_path')

        # Check results
        self.assertEqual(geom.geom_type, 'Polygon')
        self.assertDictEqual(props, {'prop_a': 'A', 'prop_b': 'B'})

    @patch('__builtin__.open', mock_open(read_data=FEATURE_GEOJSON), create=True)
    def test_valid_feature(self):
        """Tests parsing geojson"""

        # Call method to test
        geom, props = geo_utils.parse_geo_json_file('fake_path')

        # Check results
        self.assertEqual(geom.geom_type, 'Polygon')
        self.assertDictEqual(props, {'prop_a': 'A', 'prop_b': 'B'})

    @patch('__builtin__.open', mock_open(read_data=FEATURE_GEOJSON_NO_PROPS), create=True)
    def test_valid_feature_no_props(self):
        """Tests parsing geojson"""

        # Call method to test
        geom, props = geo_utils.parse_geo_json_file('fake_path')

        # Check results
        self.assertEqual(geom.geom_type, 'Polygon')
        self.assertIsNone(props)

    def test_parse_geo_json(self):
        """Tests parsing geojson"""

        geo_json = {'geometry': {'type': 'POLYGON', 'coordinates': [[[40, 26], [50, 27], [60, 26], [50, 25], [40, 26]]]}, 'type': 'Feature'}

        # Call method to test
        geom, props = geo_utils.parse_geo_json(geo_json)

        # Check results
        self.assertEqual(geom.geom_type, 'Polygon')
        self.assertIsNone(props)

    @patch('__builtin__.open', mock_open(read_data=POLYGON_GEOJSON), create=True)
    def test_valid_polygon(self):
        """Tests parsing geojson"""

        # Call method to test
        geom, props = geo_utils.parse_geo_json_file('fake_path')

        # Check results
        self.assertEqual(geom.geom_type, 'Polygon')
        self.assertIsNone(props)

    def test_get_center_point(self):
        """Tests calculating center point"""
        geo_json = {
            "type": "Polygon",
            "coordinates": [[[ 1.0, 10.0 ], [ 2.0, 10.0 ], [ 2.0, 20.0 ],[ 1.0, 20.0 ], [ 1.0, 10.0 ]]]
        }

        # Call method to test
        geom, props = geo_utils.parse_geo_json(geo_json)
        center = geo_utils.get_center_point(geom)

        # Check results
        self.assertEqual(center.geom_type, 'Point')
        self.assertEqual(center.coords, (1.5, 15.0))

    def test_parse_bad_geo_json(self):
        """Tests parsing bad geojson"""

        # Bad geom (missing repeat of first point to close polygon)
        geo_json = {'geometry': {'type': 'POLYGON', 'coordinates': [[[40, 26], [50, 27], [60, 26], [50, 25]]]}, 'type': 'Feature'}

        # Call method and check results
        self.assertRaises(InvalidResultsManifest, geo_utils.parse_geo_json, geo_json)
