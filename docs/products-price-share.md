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
- The result is rounded to two decimal places and stored as `NUMBER(5,2)`.
- When the overall total is zero, every category returns `0.00`. The model
  checks for a zero total before dividing, so it raises no division error.

With the five-row `sample_data/products.csv` fixture, Electronics totals
`1199.39` and Furniture `298.90`, out of `1498.29`. Their shares are `80.05`
and `19.95`. A single category with a positive total returns `100.00`.

Each share is rounded separately, so the shares across categories may sum
to slightly more or less than `100.00`.

## Data tests

- `not_null` and `nonnegative` on `price_share_pct`.
- `gold_price_share_pct_in_range` fails on any share above `100`.
- `gold_matches_silver` recomputes the share from Silver and fails on any
  difference.

The column uses the existing `ref('stg_products')` dependency and products
schema routing. So it is built in `PR_<number>_GOLD` for PR validation and in
`GOLD` for shared Dev and Prod. The products graph now selects 34 data tests.

Offline checks compile the model with dummy profiles and run the translated
SQL in DuckDB (`tests/test_products_dbt.py`). They cover the sample shares,
the single-category case, the zero-total case and the out-of-range checks.
The Snowflake build and tests run only in the existing CI jobs.
