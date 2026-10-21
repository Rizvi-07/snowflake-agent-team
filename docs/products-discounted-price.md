# Products discounted price

The Silver `stg_products` model includes `discounted_price`: each product's
price after a fixed 10% discount. This is a demonstration calculation. The
original `price` column and all Gold results are unchanged.

## Calculation

```sql
round(price * 0.90, 2) as discounted_price
```

The model implements this as:

```sql
cast(round(cast(price as number(38, 2)) * 0.90, 2) as number(18, 2))
```

- `price` is Silver's converted `NUMBER(18,2)` value, not the raw Bronze
  string. The model converts the Bronze columns in a `normalized` CTE and adds
  `discounted_price` in the outer select. A raw `price` reference in the same
  select would bind to the Bronze `VARCHAR` column, not the converted alias.
- The arithmetic is fixed-point decimal throughout; no floating-point values
  are involved. Widening to `NUMBER(38,2)` before multiplying keeps the exact
  four-decimal product, even for the largest `NUMBER(18,2)` price.
- `ROUND` rounds half away from zero, which is half up for nonnegative prices.
  The result is cast to `NUMBER(18,2)`, the same type as `price`.
- A null (invalid) price gives a null `discounted_price`. The row is kept and
  still fails the existing `price` tests and the new `not_null` test.
- For a valid nonnegative price, `discounted_price` is non-null, nonnegative
  and never exceeds `price`.

| `price`               | `price * 0.90`          | `discounted_price`    |
|-----------------------|-------------------------|-----------------------|
| `100.00`              | `90.0000`               | `90.00`               |
| `49.90`               | `44.9100`               | `44.91`               |
| `10.05`               | `9.0450`                | `9.05` (half up)      |
| `0.06`                | `0.0540`                | `0.05`                |
| `0.00`                | `0.0000`                | `0.00`                |
| `9999999999999999.99` | `8999999999999999.9910` | `8999999999999999.99` |

With the five-row `sample_data/products.csv` fixture, Laptop, Monitor, Desk,
Chair and Keyboard become `809.99`, `224.55`, `179.10`, `89.91` and `44.91`.

The new column comes after `ingested_at`, so existing columns keep their
positions. Gold `product_category_summary` still aggregates `price` only.

## Data tests

- `not_null` and `nonnegative` on `discounted_price`.
- `silver_discounted_price_within_price`: for every valid nonnegative price,
  fails when `discounted_price` is null, negative, above `price`, or outside
  the half-open half-cent window `[price * 0.9 - 0.005, price * 0.9 + 0.005)`
  that defines rounding half up to cents.
- `silver_discounted_price_boundaries` runs the model's own expression (the
  text between its `discounted_price` markers) on fixed prices. It compares
  the results with hand-worked literals, including exact half cents
  (`10.05`, `0.05`), rounding down (`0.06`), zero and the largest price.

The column is part of the existing `+product_category_summary` graph and the
products schema routing. PR validation builds it in `PR_<number>_SILVER`;
shared Dev and Prod build it in `SILVER`. The products graph now has 41 data
tests (43 selected nodes, including both models). All of them run in the
existing Snowflake PR validation build.

Offline checks compile the models with dummy profiles and run the translated
SQL in DuckDB (`tests/test_products_dbt.py`). They cover the acceptance
examples, the sample fixture, 200 random prices against Python's
`ROUND_HALF_UP`, the boundary test with a truncating expression substituted
(which must fail), and every data test detecting a corrupted value. DuckDB
does not reproduce Snowflake's numeric types, so only the Snowflake CI build
verifies Snowflake numeric behavior.
