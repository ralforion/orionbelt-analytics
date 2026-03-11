"""Chart generation handler implementation."""

import json
import logging
from typing import Optional, Union, List, Dict, Any

from fastmcp import Context
from fastmcp.utilities.types import Image
from mcp_ui_server import create_ui_resource
from mcp_ui_server.core import UIResource

from ..chart_utils import save_image_to_tmp

logger = logging.getLogger(__name__)


async def generate_chart(
    ctx: Context,
    data_source: Union[List[Dict[str, Any]], str],
    chart_type: str,
    x_column: str,
    y_column: Optional[Union[str, List[str]]],
    color_column: Optional[str],
    title: Optional[str],
    chart_style: str,
    width: int,
    height: int,
    sort_by: Optional[str],
    sort_order: Optional[str],
    output_format: str,
) -> Union[List[UIResource], Image]:
    """Generate interactive or static charts from query results.

    This handler delegates to tools.chart.generate_chart for the core charting
    logic and handles MCP-specific concerns (UIResource, Image, ctx.info).
    """
    from ..tools.chart import generate_chart as generate_chart_impl
    import numpy as np

    # Custom JSON encoder for numpy arrays from Plotly
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, (np.integer, np.floating)):
                return obj.item()
            return super().default(obj)

    # Parse data_source if it's a string
    if data_source and isinstance(data_source, str):
        logger.warning("data_source was sent as string instead of JSON array - attempting to parse")
        try:
            parsed = json.loads(data_source)
            if isinstance(parsed, list):
                data_source = parsed
                logger.info("Successfully parsed data_source from JSON string format")
        except (json.JSONDecodeError, ValueError):
            try:
                import ast

                parsed = ast.literal_eval(data_source)
                if isinstance(parsed, list):
                    data_source = parsed
                    logger.info("Successfully parsed data_source from Python literal string format")
            except (ValueError, SyntaxError) as e:
                await ctx.info("Chart generation failed - data_source format error")
                raise RuntimeError(
                    f"data_source must be valid JSON (array of objects), not a string. "
                    f"Received string: {data_source[:100]}... "
                    f"Expected format: [{{'key': 'value'}}, ...] "
                    f"Parse error: {str(e)}"
                )

    # Parse y_column if it's a JSON string list
    if y_column and isinstance(y_column, str):
        try:
            parsed = json.loads(y_column)
            if isinstance(parsed, list):
                y_column = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Call implementation
    logger.info(f"generate_chart called with output_format={output_format}, chart_type={chart_type}")
    result = generate_chart_impl(
        data_source,
        chart_type,
        x_column,
        y_column,
        color_column,
        title,
        chart_style,
        width,
        height,
        sort_by,
        sort_order,
        output_format=output_format,
    )
    logger.info(
        f"generate_chart_impl returned: {type(result)}, has_error={isinstance(result, dict) and 'error' in result}"
    )

    # Check for errors
    if isinstance(result, dict) and result.get("error"):
        await ctx.info("Chart generation failed")
        raise RuntimeError(result.get("error", "Chart generation failed"))

    # Handle interactive output
    if output_format == "interactive":
        if isinstance(result, dict) and "traces" in result:
            data_points = result.get("metadata", {}).get("data_points", 0)
            chart_id = result.get("metadata", {}).get("chart_id", "chart")

            chart_json = json.dumps(result, cls=NumpyEncoder)

            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: system-ui, sans-serif; background: #fff; }}
        #chart {{ width: 100%; height: 100vh; min-height: 400px; }}
    </style>
</head>
<body>
    <div id="chart"></div>
    <script>
        const chartData = {chart_json};
        const {{ traces, layout, config }} = chartData;
        const finalConfig = {{
            displayModeBar: true,
            responsive: true,
            scrollZoom: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['lasso2d', 'select2d'],
            ...config
        }};
        const finalLayout = {{
            autosize: true,
            margin: {{ l: 60, r: 60, t: 60, b: 60 }},
            ...layout
        }};
        Plotly.newPlot('chart', traces, finalLayout, finalConfig);
        window.addEventListener('resize', () => Plotly.Plots.resize(document.getElementById('chart')));
    </script>
</body>
</html>"""

            ui_resource = create_ui_resource(
                {
                    "uri": f"ui://orionbelt/chart/{chart_id}",
                    "content": {"type": "rawHtml", "htmlString": html_content},
                    "encoding": "text",
                }
            )

            await ctx.info(f"Interactive chart generated: {chart_type} with {data_points} data points")
            return [ui_resource]
        else:
            await ctx.info("Chart generation failed")
            raise RuntimeError("Chart generation failed: unexpected result format for interactive mode")

    # Handle image output
    if isinstance(result, tuple) and len(result) == 2:
        image_bytes, chart_id = result

        # Get connection_id from session for scoped storage
        from ..main import get_session_data
        session = get_session_data(ctx)
        connection_id = session.connection_id

        image_file_path = save_image_to_tmp(
            image_bytes,
            chart_id,
            "png",
            connection_id=connection_id
        )

        if not image_file_path:
            await ctx.info("Chart generation failed to save file")
            raise RuntimeError("Failed to save chart image to file")

        await ctx.info(f"Chart image generated successfully: {image_file_path}")
        return Image(path=str(image_file_path))
    else:
        await ctx.info("Chart generation failed")
        raise RuntimeError("Chart generation failed: unexpected result format")
