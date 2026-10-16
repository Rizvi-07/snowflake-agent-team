# Products category price share

The Gold `product_category_summary` model includes `price_share_pct`: each
category's share of the total product price across all categories.

## Calculation

```text
price_share_pct = category total price / total price across all categories × 100
```

- `category total price` is the existing `total_price` column, the sum of the
  category's Silver `NUMBER(18,2)` prices.
- The overall total is the sum of `total_price` over every Gold row, computed
  with a window function in the same model.
- The result is stored as `NUMBER(5,2)`.
- When the overall total is zero, every category returns `0.00`. The model
  checks for a zero total before dividing, so it raises no division error.

With the five-row `sample_data/products.csv` fixture, Electronics totals
`1199.39` and Furniture `298.90`, out of `1498.29`. Their shares are `80.05`
and `19.95`. A single category with a positive total returns `100.00`.

Each share is rounded separately, so the shares across categories may sum
to slightly more or less than `100.00`.

## Rounding

The share is rounded once, to two decimal places. A value exactly halfway
between hundredths rounds up (for example `0.005` becomes `0.01`).

The model does not round a quotient and then round again. In Snowflake,
`total_price * 100 / overall_total_price` is a fixed-point division that keeps
only a limited number of decimal places. That intermediate rounding can move a
value just below a half-hundredth onto it. With category totals `100.00` and
`1999900.01`, the exact share is `0.004999999975…`. Rounded to eight places it
is `0.00500000`, which a second rounding turns into `0.01` instead of `0.00`.
A larger scale only moves the problem to other inputs, and `FLOAT` is inexact.

Instead, the model counts hundredths of a percent with exact quotient and
remainder arithmetic:

```text
numerator  = total_price × 10000
hundredths = (numerator − MOD(numerator, overall)) / overall
           + 1 when 2 × MOD(numerator, overall) ≥ overall, else 0
price_share_pct = hundredths × 0.01
```

`numerator − MOD(numerator, overall)` is an exact multiple of `overall`, so the
division has an integer result and no digits are discarded. The remainder
decides the half-up step exactly. Multiplying by `0.01` adds scale instead of
dividing, so the final value is exact before the cast to `NUMBER(5,2)`.

### Range and overflow

Snowflake fixed-point values hold at most 38 digits. `SUM` of `NUMBER(18,2)`
prices returns `NUMBER(38,2)`. The largest intermediate value is
`total_price × 10000`, which also has scale 2, so it fits while a category
total is below 10^32. The quotient is at most 10000, and `2 × MOD(...)` is
below twice the overall total. Real product totals are far below this bound.
If it were exceeded, Snowflake is expected to fail the build with a numeric
overflow error rather than return a wrong share; CI has not exercised that.

Shares are defined for nonnegative prices. Negative Silver prices already
fail the `nonnegative` data tests on the Silver and Gold price columns.

## Data tests

- `not_null` and `nonnegative` on `price_share_pct`.
- `gold_price_share_pct_in_range` fails on any share above `100`.
- `gold_matches_silver` recomputes the totals from Silver and checks each
  share without dividing or rounding. A two-decimal share `p` is correct
  exactly when
  `(p − 0.005) × overall ≤ total × 100 < (p + 0.005) × overall`.
- `gold_price_share_pct_boundaries` runs the model's own share expression,
  taken from between the `price_share_pct:begin` and `price_share_pct:end`
  markers in the model, over fixed category totals. The expected shares are
  literals worked out separately. Cases cover the sample data, a single
  category, a zero total, and values immediately below, exactly at and
  immediately above a half-hundredth (including `100.00` and `1999900.01`,
  which must give `0.00` and `100.00`). Compilation fails if the markers are
  missing or repeated.

The column uses the existing `ref('stg_products')` dependency and products
schema routing. So it is built in `PR_<number>_GOLD` for PR validation and in
`GOLD` for shared Dev and Prod. The products graph now selects 35 data tests,
all of which run in the existing Snowflake PR validation build.

Offline checks compile the model with dummy profiles and run the translated
SQL in DuckDB (`tests/test_products_dbt.py`). They cover the sample shares,
the single-category and zero-total cases, randomized totals against an exact
`Fraction` calculation, and that the boundary test rejects the earlier
double-rounding expression. DuckDB does not reproduce Snowflake's numeric
types, so only the Snowflake CI build verifies Snowflake numeric behavior.
