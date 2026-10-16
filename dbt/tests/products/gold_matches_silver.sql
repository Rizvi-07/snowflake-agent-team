{{ config(tags=['products']) }}

with silver as (
    select
        category,
        count(*) as product_count,
        sum(price) as total_price,
        cast(avg(price) as number(38, 6)) as average_price,
        min(price) as minimum_price,
        max(price) as maximum_price
    from {{ ref('stg_products') }}
    group by category
), expected as (
    select
        *,
        cast(
            case
                when sum(total_price) over () = 0 then 0
                else round(total_price * 100 / sum(total_price) over (), 2)
            end
            as number(5, 2)
        ) as price_share_pct
    from silver
), actual as (
    select * from {{ ref('product_category_summary') }}
)
select
    expected.category as expected_category,
    actual.category as actual_category,
    expected.product_count as expected_count,
    actual.product_count as actual_count,
    expected.total_price as expected_total,
    actual.total_price as actual_total
from expected
full outer join actual on expected.category is not distinct from actual.category
where expected.product_count is distinct from actual.product_count
    or expected.total_price is distinct from actual.total_price
    or expected.average_price is distinct from actual.average_price
    or expected.minimum_price is distinct from actual.minimum_price
    or expected.maximum_price is distinct from actual.maximum_price
    or expected.price_share_pct is distinct from actual.price_share_pct
