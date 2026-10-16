with category_totals as (
    select
        category,
        count(*) as product_count,
        sum(price) as total_price,
        cast(avg(price) as number(38, 6)) as average_price,
        min(price) as minimum_price,
        max(price) as maximum_price
    from {{ ref('stg_products') }}
    group by category
), overall as (
    select
        *,
        sum(total_price) over () as overall_total_price
    from category_totals
)
select
    category,
    product_count,
    total_price,
    average_price,
    minimum_price,
    maximum_price,
    -- Share of the total price across all categories; 0.00 when that total is zero.
    cast(
        case
            when overall_total_price = 0 then 0
            else round(total_price * 100 / overall_total_price, 2)
        end
        as number(5, 2)
    ) as price_share_pct
from overall
