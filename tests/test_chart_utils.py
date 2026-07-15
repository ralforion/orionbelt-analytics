"""Coverage tests for chart_utils pure transforms and chart construction."""

import unittest

import pandas as pd

from src.chart_utils import (
    create_plotly_chart,
    format_measure_name,
    get_quarter_sort_order,
)


class TestChartHelpers(unittest.TestCase):
    def test_format_measure_name(self):
        self.assertEqual(format_measure_name("total_revenue"), "Total Revenue")
        self.assertEqual(format_measure_name("profit_margin_pct"), "Profit Margin Pct")

    def test_get_quarter_sort_order_orders_chronologically(self):
        values = ["Q3 2023", "Q1 2023", "Q2 2023", "Q4 2023"]
        ordered = get_quarter_sort_order(values, ascending=True)
        self.assertEqual(list(ordered), ["Q1 2023", "Q2 2023", "Q3 2023", "Q4 2023"])


class TestCreatePlotlyChart(unittest.TestCase):
    def _bar_df(self):
        return pd.DataFrame({"category": ["A", "B", "C"], "value": [3, 1, 2]})

    def test_bar_chart(self):
        fig = create_plotly_chart(
            self._bar_df(), "bar", "category", "value", None, "Bar", "grouped"
        )
        self.assertIsNotNone(fig)
        self.assertTrue(hasattr(fig, "data"))

    def test_line_chart(self):
        df = pd.DataFrame({"month": ["Jan", "Feb", "Mar"], "value": [1, 2, 3]})
        fig = create_plotly_chart(df, "line", "month", "value", None, "Line", "grouped")
        self.assertIsNotNone(fig)

    def test_line_chart_datetime_x_axis_is_typed_as_date(self):
        """A datetime x-axis must be marked type='date', at any resolution.

        Asserts the axis config rather than just a non-None figure, which is
        what let the resolution bug hide. Note this only bites under pandas 3
        (datetime64[us]); on pandas 2 the line path re-parses x to [ns], so it
        passes either way.
        """
        df = pd.DataFrame(
            {
                "day": pd.to_datetime(["2024-01-15", "2024-02-15", "2024-03-15"]),
                "value": [1, 2, 3],
            }
        )
        fig = create_plotly_chart(df, "line", "day", "value", None, "Line", "grouped")
        self.assertEqual(fig.layout.xaxis.type, "date")

    def test_scatter_chart(self):
        df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
        fig = create_plotly_chart(df, "scatter", "x", "y", None, "Scatter", "grouped")
        self.assertIsNotNone(fig)

    def test_heatmap_chart(self):
        df = pd.DataFrame(
            {
                "region": ["N", "N", "S", "S"],
                "product": ["X", "Y", "X", "Y"],
                "sales": [10, 20, 30, 40],
            }
        )
        fig = create_plotly_chart(
            df, "heatmap", "region", "product", "sales", "Heatmap", "grouped"
        )
        self.assertIsNotNone(fig)

    def test_multi_series_bar(self):
        df = pd.DataFrame({"q": ["Q1", "Q2"], "rev": [5, 7], "cost": [2, 3]})
        fig = create_plotly_chart(
            df, "bar", "q", ["rev", "cost"], None, "Multi", "stacked"
        )
        self.assertIsNotNone(fig)


if __name__ == "__main__":
    unittest.main()
