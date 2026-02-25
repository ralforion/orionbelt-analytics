# Chart Generation Examples

**Skill for OrionBelt Analytics - Visualization Guide**

## Overview

The `generate_chart` tool creates interactive and static visualizations from SQL query results. This guide provides examples for all supported chart types.

---

## Data Format

Charts expect data as a list of dictionaries (JSON array of objects):

```json
[
    {"country": "USA", "customer_count": 5, "revenue": 10000},
    {"country": "Germany", "customer_count": 3, "revenue": 7500},
    {"country": "UK", "customer_count": 4, "revenue": 9000}
]
```

This is the format returned by `execute_sql_query()` in the `data` field.

---

## Chart Types and Examples

### 1. Simple Bar Chart

**Best for:** Comparing values across categories

```python
# Query data
result = execute_sql_query("""
    SELECT
        public.orders.category,
        SUM(public.orders.sales) as total
    FROM public.orders
    GROUP BY public.orders.category
""")

# Generate bar chart
generate_chart(
    data_source=result['data'],
    chart_type='bar',
    x_column='category',
    y_column='total',
    title='Sales by Category'
)
```

### 2. Stacked Bar Chart

**Best for:** Showing composition across categories

```python
result = execute_sql_query("""
    SELECT
        public.sales.region,
        public.sales.product_type,
        SUM(public.sales.revenue) as total
    FROM public.sales
    GROUP BY public.sales.region, public.sales.product_type
""")

generate_chart(
    data_source=result['data'],
    chart_type='bar',
    x_column='region',
    y_column='total',
    color_column='product_type',
    chart_style='stacked',
    title='Revenue by Region and Product Type'
)
```

### 3. Grouped Bar Chart

**Best for:** Side-by-side comparison of categories

```python
result = execute_sql_query("""
    SELECT
        public.sales.region,
        public.sales.product,
        SUM(public.sales.quantity) as units
    FROM public.sales
    GROUP BY public.sales.region, public.sales.product
""")

generate_chart(
    data_source=result['data'],
    chart_type='bar',
    x_column='region',
    y_column='units',
    color_column='product',
    chart_style='grouped',
    title='Units Sold by Region and Product'
)
```

### 4. Time Series Line Chart (Single Measure)

**Best for:** Showing trends over time

```python
result = execute_sql_query("""
    SELECT
        public.daily_sales.date,
        public.daily_sales.revenue
    FROM public.daily_sales
    ORDER BY public.daily_sales.date
""")

generate_chart(
    data_source=result['data'],
    chart_type='line',
    x_column='date',
    y_column='revenue',
    title='Revenue Trend Over Time'
)
```

### 5. Multi-Measure Line Chart

**Best for:** Comparing multiple metrics over time

```python
result = execute_sql_query("""
    SELECT
        public.monthly_data.month,
        public.monthly_data.revenue,
        public.monthly_data.expenses,
        public.monthly_data.profit
    FROM public.monthly_data
    ORDER BY public.monthly_data.month
""")

generate_chart(
    data_source=result['data'],
    chart_type='line',
    x_column='month',
    y_column=['revenue', 'expenses', 'profit'],  # List of measures
    title='Financial Metrics Comparison'
)
```

### 6. Scatter Plot

**Best for:** Showing relationships between two continuous variables

```python
result = execute_sql_query("""
    SELECT
        public.products.price,
        public.products.quality_score
    FROM public.products
""")

generate_chart(
    data_source=result['data'],
    chart_type='scatter',
    x_column='price',
    y_column='quality_score',
    title='Price vs Quality Analysis'
)
```

### 7. Scatter Plot with Categories

**Best for:** Showing relationships grouped by category

```python
result = execute_sql_query("""
    SELECT
        public.products.price,
        public.products.quality_score,
        public.products.brand
    FROM public.products
""")

generate_chart(
    data_source=result['data'],
    chart_type='scatter',
    x_column='price',
    y_column='quality_score',
    color_column='brand',
    title='Price vs Quality by Brand'
)
```

### 8. Correlation Heatmap

**Best for:** Visualizing correlations between numeric columns

```python
result = execute_sql_query("""
    SELECT
        public.metrics.metric1,
        public.metrics.metric2,
        public.metrics.metric3,
        public.metrics.metric4
    FROM public.metrics
""")

generate_chart(
    data_source=result['data'],
    chart_type='heatmap',
    title='Metric Correlations'
)
```

### 9. Simple Example with Explicit Data

**For testing or small datasets:**

```python
generate_chart(
    data_source=[
        {"country": "USA", "customer_count": 5},
        {"country": "Germany", "customer_count": 3},
        {"country": "UK", "customer_count": 4},
        {"country": "France", "customer_count": 2},
        {"country": "Japan", "customer_count": 1}
    ],
    chart_type="bar",
    x_column="country",
    y_column="customer_count",
    title="Customers by Country"
)
```

---

## Styling Options

### Chart Styles

- **`chart_style='default'`** - Standard visualization
- **`chart_style='stacked'`** - Stack bars/areas (for bar/area charts)
- **`chart_style='grouped'`** - Group bars side-by-side (for bar charts)

### Color Column

Add a `color_column` parameter to:
- Create stacked or grouped bar charts
- Color scatter plot points by category
- Add series to line charts

Example:
```python
generate_chart(
    data_source=result['data'],
    chart_type='bar',
    x_column='region',
    y_column='sales',
    color_column='product_category'  # ← Colors/groups by category
)
```

### Titles

Always add descriptive titles:
```python
generate_chart(
    ...,
    title='Q4 2024 Sales Performance by Region'
)
```

---

## Best Practices

1. **Order data appropriately** - Use `ORDER BY` in SQL for time series and ordered categories
2. **Limit data size** - Charts with 100+ points may be slow; consider aggregation
3. **Choose the right chart type:**
   - Bar: Categorical comparisons
   - Line: Trends over time
   - Scatter: Relationships between variables
   - Heatmap: Correlations or patterns
4. **Use descriptive titles** - Help users understand what they're looking at
5. **Test with small data first** - Verify query results before charting large datasets

---

## Typical Workflow

```python
# 1. Execute SQL query
result = execute_sql_query("""
    SELECT
        public.sales.month,
        public.sales.region,
        SUM(public.sales.revenue) as total_revenue
    FROM public.sales
    WHERE public.sales.year = 2024
    GROUP BY public.sales.month, public.sales.region
    ORDER BY public.sales.month
""")

# 2. Generate chart from results
generate_chart(
    data_source=result['data'],
    chart_type='line',
    x_column='month',
    y_column='total_revenue',
    color_column='region',
    title='2024 Monthly Revenue by Region'
)
```

---

## Output Formats

### Interactive Mode (Default)
- Returns JSON data for client-side rendering
- Uses MCP Apps protocol
- Rendered interactively in Claude Desktop
- Supports zoom, pan, hover tooltips

### Image Mode (Fallback)
- Generates static PNG images
- Saved to `tmp/` directory
- Uses Matplotlib for rendering
- Suitable for reports and documentation

---

## Error Handling

Common errors and solutions:

| Error | Cause | Solution |
|-------|-------|----------|
| "Column not found" | Typo in column name | Check `result['columns']` for actual names |
| "Data type mismatch" | Non-numeric data in numeric field | Ensure aggregation in SQL or cast to number |
| "No data to plot" | Empty result set | Verify SQL query returns data |
| "Too many data points" | Dataset too large | Add LIMIT or aggregate data in SQL |

---

## Quick Reference

| Chart Type | X Column | Y Column | Color Column | Use Case |
|------------|----------|----------|--------------|----------|
| bar | Category | Numeric | Optional | Compare categories |
| line | Date/Time | Numeric or [Numeric] | Optional | Show trends |
| scatter | Numeric | Numeric | Optional | Show relationships |
| heatmap | N/A | N/A | N/A | Show correlations |

---

**Remember:** Always execute your SQL query first, then pass `result['data']` to `generate_chart`!
