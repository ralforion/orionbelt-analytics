# Fan-Trap Prevention Guide

**Skill for OrionBelt Analytics MCP Server**

## What is a Fan-Trap?

A fan-trap occurs when a parent table has multiple 1:many relationships and you JOIN them with aggregation, causing data multiplication (Cartesian product).

### Example:
```
orders (1) → order_items (many)
orders (1) → shipments (many)
```

**❌ WRONG APPROACH:**
```sql
SELECT SUM(public.order_items.amount)
FROM public.orders
JOIN public.order_items ON public.orders.id = public.order_items.order_id
JOIN public.shipments ON public.orders.id = public.shipments.order_id;
```

**Result:** Inflated totals due to Cartesian product multiplication

**✅ CORRECT APPROACH:** Use UNION ALL to combine facts, then aggregate

---

## Detection Checklist

Before writing multi-table queries with aggregation:

1. **Review foreign_keys** from `discover_schema()` FIRST
2. **Identify relationship patterns:**
   - Safe: 1:1 relationships (customers → customer_profiles)
   - Requires care: 1:many (customers → orders)
   - High risk: Multiple 1:many from same parent (fan-trap potential)
3. **Use `validate_sql_syntax()`** before execution
4. **Validate results** against source tables

---

## Safe Query Patterns

### PATTERN 1 - UNION ALL (RECOMMENDED)

**Best for:** Combining multiple fact tables with consistent measures

```sql
WITH unified_facts AS (
    SELECT
        public.fact1.key,
        public.fact1.category,
        public.fact1.amount as measure_value,
        'type1' as fact_type
    FROM public.fact1

    UNION ALL

    SELECT
        public.fact2.key,
        public.fact2.category,
        public.fact2.quantity as measure_value,
        'type2' as fact_type
    FROM public.fact2
)
SELECT
    key,
    category,
    SUM(measure_value) as total_measure,
    COUNT(DISTINCT fact_type) as num_fact_types
FROM unified_facts
GROUP BY key, category;
```

**Benefits:**
- No data multiplication
- Unified data model for consistent aggregation
- Easy to extend with additional fact types
- Better performance with fewer table scans

### PATTERN 2 - SEPARATE AGGREGATION

**Use when:** UNION approach is not suitable (different measures)

```sql
WITH fact1_totals AS (
    SELECT
        public.fact1.key,
        SUM(public.fact1.amount) as total_amount
    FROM public.fact1
    GROUP BY public.fact1.key
),
fact2_totals AS (
    SELECT
        public.fact2.key,
        SUM(public.fact2.quantity) as total_quantity
    FROM public.fact2
    GROUP BY public.fact2.key
)
SELECT
    f1.key,
    f1.total_amount,
    COALESCE(f2.total_quantity, 0) as total_quantity
FROM fact1_totals f1
LEFT JOIN fact2_totals f2 ON f1.key = f2.key;
```

### PATTERN 3 - DISTINCT AGGREGATION (USE CAREFULLY)

**Warning:** Only use when you fully understand the data relationships

```sql
SELECT
    public.fact1.key,
    SUM(DISTINCT public.fact1.amount) as total_amount,
    SUM(public.fact2.quantity) as total_quantity
FROM public.fact1
LEFT JOIN public.fact2 ON public.fact1.id = public.fact2.fact1_id
GROUP BY public.fact1.key;
```

**Caution:** DISTINCT can mask issues and give false confidence

### PATTERN 4 - WINDOW FUNCTIONS

**For:** Complex analytical queries with preserved granularity

```sql
SELECT DISTINCT
    public.fact1.key,
    SUM(public.fact1.amount) OVER (PARTITION BY public.fact1.key) as total_amount,
    f2.pre_aggregated_quantity
FROM public.fact1
LEFT JOIN (
    SELECT
        public.fact2.key,
        SUM(public.fact2.qty) as pre_aggregated_quantity
    FROM public.fact2
    GROUP BY public.fact2.key
) f2 ON public.fact1.key = f2.key;
```

---

## Common Problematic Combinations

**Patterns requiring careful review:**

- `public.sales LEFT JOIN public.shipments + SUM(public.sales.amount)`
- `public.orders LEFT JOIN public.order_items LEFT JOIN public.products + SUM(public.orders.total)`
- `public.customers LEFT JOIN public.transactions LEFT JOIN public.transaction_items + aggregation`
- Queries joining parent→child1 + parent→child2 with SUM/COUNT

---

## Relationship Examples

### Safe (1:1 relationships):
```
customers → customer_profiles (1:1)
employees → employee_details (1:1)
```

### Requires care (1:many):
```
customers → orders (1:many)
products → inventory_records (1:many)
```

### High risk (fan-trap potential):
```
orders → order_items (1:many) + orders → shipments (1:many)
customers → orders (1:many) + customers → support_tickets (1:many)
```

**For high-risk patterns:** Always use UNION approach or separate aggregation CTEs

---

## Fan-Trap Solutions

If you suspect fan-trap in existing query:

1. **Split into UNION approach** (recommended)
2. **Use separate aggregations** with CTEs
3. **Add DISTINCT in SUM()** as temporary fix (not ideal)
4. **Validate results** against source tables
5. **Aggregate fact tables separately** before joining

**Critical:** Fan-traps cause silent data corruption - queries execute successfully but return inflated results!

---

## Result Validation

**Verify results make business sense:**

- Compare totals with business expectations
- Cross-check: `SELECT SUM(public.base_table.amount) FROM public.base_table` vs your query result
- Ensure row counts are reasonable
- High/unexpected results may indicate fan-trap multiplication

---

## Validation Checklist

For queries with 2+ tables and aggregation:

- [ ] Schema analyzed with `discover_schema()`
- [ ] Relationships reviewed (check foreign_keys)
- [ ] Fan-trap patterns identified
- [ ] Syntax validated with `validate_sql_syntax()`
- [ ] Safe aggregation pattern selected
- [ ] Results validated against business expectations

---

## Quick Reference

| Scenario | Solution |
|----------|----------|
| Multiple fact tables, same measures | UNION ALL (Pattern 1) |
| Multiple fact tables, different measures | Separate aggregation (Pattern 2) |
| Single fact table | Direct aggregation (no fan-trap risk) |
| 1:1 relationships only | Direct JOIN (safe) |
| Parent + multiple child tables | UNION or separate CTEs |

---

**Always remember:** When in doubt, use UNION ALL or separate aggregations!
