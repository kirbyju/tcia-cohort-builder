import unittest

import pandas as pd

from fetch_data import IDC_GEOMETRY_COLUMNS, load_volume_geometry_index


class FakeIDCClient:
    def __init__(self, geometry: pd.DataFrame | None = None):
        self.geometry = geometry
        self.fetch_calls: list[str] = []

    def fetch_index(self, name: str) -> None:
        self.fetch_calls.append(name)
        if self.geometry is not None:
            self.volume_geometry_index = self.geometry


class VolumeGeometryIndexTests(unittest.TestCase):
    def test_fetches_optional_index_before_using_it(self):
        row = {"SeriesInstanceUID": "1.2.3"}
        row.update({column: True for column in IDC_GEOMETRY_COLUMNS})
        client = FakeIDCClient(pd.DataFrame([row]))

        geometry = load_volume_geometry_index(client)

        self.assertEqual(client.fetch_calls, ["volume_geometry_index"])
        self.assertEqual(geometry["SeriesInstanceUID"].tolist(), ["1.2.3"])

    def test_missing_optional_index_has_operator_error(self):
        client = FakeIDCClient()

        with self.assertRaisesRegex(RuntimeError, "returned no rows"):
            load_volume_geometry_index(client)

    def test_missing_geometry_column_has_operator_error(self):
        client = FakeIDCClient(pd.DataFrame([{"SeriesInstanceUID": "1.2.3"}]))

        with self.assertRaisesRegex(RuntimeError, "missing required columns"):
            load_volume_geometry_index(client)


if __name__ == "__main__":
    unittest.main()
