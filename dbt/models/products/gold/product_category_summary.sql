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
    -- Share of the total price across all categories, rounded half up to
    -- hundredths; 0.00 when that total is zero. Exact quotient/remainder
    -- arithmetic counts hundredths of a percent without rounding a quotient:
    -- hundredths = floor(total * 10000 / overall), plus 1 when the remainder
    -- is at least half the overall total. See docs/products-price-share.md.
    -- gold_price_share_pct_boundaries reuses the text between the markers.
    -- price_share_pct:begin
    cast(
        case
            when overall_total_price = 0 then 0
            else (
                (total_price * 10000 - mod(total_price * 10000, overall_total_price))
                    / overall_total_price
                + case
                    when mod(total_price * 10000, overall_total_price) * 2 >= overall_total_price then 1
                    else 0
                end
            ) * 0.01
        end
        as number(5, 2)
    )
    -- price_share_pct:end
    as price_share_pct,
    -- Exact fixed-point difference; 0.00 for a single product or equal prices.
    -- See docs/products-price-range.md.
    maximum_price - minimum_price as price_range
from overall
