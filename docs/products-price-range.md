# Products category price range

The Gold `product_category_summary` model includes `price_range`: the spread
between the most and least expensive product in each category.

## Calculation

```text
price_range = maximum_price - minimum_price
```

- `maximum_price` and `minimum_price` are the existing Gold columns, the `MAX`
  and `MIN` of the category's Silver `NUMBER(18,2)` prices.
- The subtraction is exact fixed-point arithmetic with two decimal places. No
  division or rounding is involved. In Snowflake, subtracting two
  `NUMBER(18,2)` values returns `NUMBER(19,2)`.
- A category with one product, or whose products all have the same price,
  returns `0.00`.
- For valid (non-null, nonnegative) Silver prices, the range is non-null and
  nonnegative, because the maximum is never below the minimum.

With the five-row `sample_data/products.csv` fixture:

| Category    | Maximum  | Minimum | `price_range` |
|-------------|----------|---------|---------------|
| Electronics | `899.99` | `49.90` | `850.09`      |
| Furniture   | `199.00` | `99.90` | `99.10`       |

The column is added after `price_share_pct`. All existing columns and their
calculations, including `price_share_pct`, are unchanged.

## Data tests

- `not_null` and `nonnegative` on `price_range`.
- `gold_matches_silver` recomputes the minimum and maximum from Silver and
  fails when `price_range` differs from their difference (including null).

The column uses the existing `ref('stg_products')` dependency and products
schema routing, so it is built in `PR_<number>_GOLD` for PR validation and in
`GOLD` for shared Dev and Prod. The products graph now selects 37 data tests,
all of which run in the existing Snowflake PR validation build.

Offline checks compile the model with dummy profiles and run the translated
SQL in DuckDB (`tests/test_products_dbt.py`). They cover the sample ranges, a
single-product category, equal-price categories, and that the data tests
reject a negative, null or corrupted range. DuckDB does not reproduce
Snowflake's numeric types, so only the Snowflake CI build verifies Snowflake
numeric behavior.
