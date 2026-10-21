{{ config(tags=['products']) }}

-- For every valid nonnegative price, the discounted price is present, is
-- nonnegative, never exceeds the original price, and is price * 0.9 rounded
-- half up to cents (within the half-open half-cent window around it).
select *
from {{ ref('stg_products') }}
where price >= 0
    and (
        discounted_price is null
        or discounted_price < 0
        or discounted_price > price
        -- Widened so the half-cent window is exact for the largest prices.
        or not (
            cast(discounted_price as number(38, 2)) - 0.005 <= cast(price as number(38, 2)) * 0.9
            and cast(price as number(38, 2)) * 0.9 < cast(discounted_price as number(38, 2)) + 0.005
        )
    )
