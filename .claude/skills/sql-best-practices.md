# SQL Best Practices for OrionBelt Analytics

**Skill for safe and effective SQL query construction**

## Identifier Qualification - CRITICAL

**ALWAYS fully qualify table and column identifiers with schema prefix** to avoid ambiguity and errors.

### ✅ CORRECT:
```sql
SELECT
    public.customers.customer_id,
    public.customers.name,
    public.orders.order_date
FROM public.customers
JOIN public.orders ON public.customers.customer_id = public.orders.customer_id
```

### ❌ WRONG:
```sql
SELECT customer_id, name, order_date
FROM customers
JOIN orders ON customers.customer_id = orders.customer_id
```

---

## Why Qualification Matters

- **Prevents ambiguous column references** in JOINs
- **Avoids schema search path issues** across different database systems
- **Ensures consistency** regardless of current schema context
- **Makes queries maintainable** and self-documenting
- **Required for cross-schema queries**

---

## Best Practices

### 1. Always Use Full Qualification

**Format:**
- SELECT clause: `schema_name.table_name.column_name`
- FROM/JOIN clauses: `schema_name.table_name`

**Example:**
```sql
SELECT
    public.products.product_id,
    public.products.name,
    public.categories.category_name
FROM public.products
JOIN public.categories ON public.products.category_id = public.categories.category_id
```

### 2. Use Table Aliases for Readability

Combine qualified names with aliases:

```sql
SELECT
    c.customer_id,
    c.name,
    o.order_date,
    o.total_amount
FROM public.customers AS c
JOIN public.orders AS o ON c.customer_id = o.customer_id
WHERE o.order_date >= '2024-01-01'
```

### 3. Qualify Even Single-Table Queries

```sql
-- ✅ GOOD
SELECT public.customers.customer_id, public.customers.name
FROM public.customers
WHERE public.customers.status = 'active'

-- ❌ AVOID
SELECT customer_id, name
FROM customers
WHERE status = 'active'
```

---

## Common Query Patterns

### Simple SELECT
```sql
SELECT
    public.table_name.column1,
    public.table_name.column2,
    public.table_name.column3
FROM public.table_name
WHERE public.table_name.status = 'active'
LIMIT 100;
```

### JOIN with Aggregation
```sql
SELECT
    public.customers.customer_id,
    public.customers.name,
    COUNT(public.orders.order_id) as order_count,
    SUM(public.orders.total_amount) as total_spent
FROM public.customers
LEFT JOIN public.orders ON public.customers.customer_id = public.orders.customer_id
GROUP BY public.customers.customer_id, public.customers.name
ORDER BY total_spent DESC
LIMIT 50;
```

### Subquery Pattern
```sql
SELECT
    public.customers.customer_id,
    public.customers.name,
    recent_orders.order_count
FROM public.customers
JOIN (
    SELECT
        public.orders.customer_id,
        COUNT(*) as order_count
    FROM public.orders
    WHERE public.orders.order_date >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY public.orders.customer_id
) recent_orders ON public.customers.customer_id = recent_orders.customer_id;
```

### CTE (Common Table Expression) Pattern
```sql
WITH active_customers AS (
    SELECT
        public.customers.customer_id,
        public.customers.name,
        public.customers.region
    FROM public.customers
    WHERE public.customers.status = 'active'
),
customer_orders AS (
    SELECT
        public.orders.customer_id,
        COUNT(*) as order_count,
        SUM(public.orders.total_amount) as total_amount
    FROM public.orders
    WHERE public.orders.order_date >= '2024-01-01'
    GROUP BY public.orders.customer_id
)
SELECT
    ac.customer_id,
    ac.name,
    ac.region,
    COALESCE(co.order_count, 0) as order_count,
    COALESCE(co.total_amount, 0) as total_amount
FROM active_customers ac
LEFT JOIN customer_orders co ON ac.customer_id = co.customer_id;
```

---

## Query Checklist

Before executing any query:

- [ ] All table references use `schema.table` format
- [ ] All column references use `schema.table.column` format (or alias.column)
- [ ] Aliases defined where appropriate for readability
- [ ] JOINs use fully qualified column names in ON clauses
- [ ] WHERE clauses use fully qualified column names
- [ ] GROUP BY uses fully qualified names or aliases
- [ ] ORDER BY uses fully qualified names or aliases

---

## Cross-Schema Queries

When querying across schemas:

```sql
SELECT
    schema1.table1.id,
    schema1.table1.name,
    schema2.table2.description
FROM schema1.table1
JOIN schema2.table2 ON schema1.table1.related_id = schema2.table2.id
```

---

## Window Functions

```sql
SELECT
    public.sales.product_id,
    public.sales.sale_date,
    public.sales.amount,
    SUM(public.sales.amount) OVER (
        PARTITION BY public.sales.product_id
        ORDER BY public.sales.sale_date
    ) as running_total,
    ROW_NUMBER() OVER (
        PARTITION BY public.sales.product_id
        ORDER BY public.sales.amount DESC
    ) as rank_within_product
FROM public.sales;
```

---

## Quick Reference

| Element | Qualification Format | Example |
|---------|---------------------|---------|
| Table in FROM | `schema.table` | `FROM public.customers` |
| Table in JOIN | `schema.table` | `JOIN public.orders` |
| Column in SELECT | `schema.table.column` or `alias.column` | `SELECT public.customers.name` |
| Column in WHERE | `schema.table.column` or `alias.column` | `WHERE public.orders.status = 'shipped'` |
| Column in JOIN ON | `schema.table.column` | `ON public.customers.id = public.orders.customer_id` |
| Column in GROUP BY | `schema.table.column` or `alias.column` | `GROUP BY public.customers.region` |

---

**Remember:** When in doubt, fully qualify everything!
