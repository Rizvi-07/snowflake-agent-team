{{ config(tags=['products']) }}

-- Category price shares are percentages and can never exceed 100.
select *
from {{ ref('product_category_summary') }}
where price_share_pct < 0
    or price_share_pct > 100
