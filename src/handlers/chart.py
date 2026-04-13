"""Chart generation handler implementation."""

import json
import logging
from typing import Optional, Union, List, Dict, Any

from fastmcp import Context

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
    get_session_data=None,
) -> str:
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

    # Handle interactive output - return JSON chart data for MCP Apps viewer
    if output_format == "interactive":
        if isinstance(result, dict) and "traces" in result:
            data_points = result.get("metadata", {}).get("data_points", 0)
            chart_type_display = result.get("metadata", {}).get("chart_type", chart_type)

            # Return the chart data as JSON string
            # The MCP Apps framework will pass this to the chart viewer iframe
            chart_json = json.dumps(result, cls=NumpyEncoder)

            await ctx.info(f"Interactive chart generated: {chart_type_display} with {data_points} data points")
            return chart_json
        else:
            await ctx.info("Chart generation failed")
            raise RuntimeError("Chart generation failed: unexpected result format for interactive mode")

    # Handle image output
    if isinstance(result, tuple) and len(result) == 2:
        image_bytes, chart_id = result

        # Get connection_id from session for scoped storage
        session = get_session_data(ctx) if get_session_data else None
        connection_id = session.connection_id if session else None

        image_file_path = save_image_to_tmp(
            image_bytes,
            chart_id,
            "png",
            connection_id=connection_id
        )

        if not image_file_path:
            await ctx.info("Chart generation failed to save file")
            raise RuntimeError("Failed to save chart image to file")

        await ctx.info(f"Chart image saved: {image_file_path}")
        return f"Chart saved to: {image_file_path}"
    else:
        await ctx.info("Chart generation failed")
        raise RuntimeError("Chart generation failed: unexpected result format")
