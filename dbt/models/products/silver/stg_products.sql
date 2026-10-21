-- Preserve every row, including failed conversions and unknown categories.
with normalized as (
    select
        case
            when regexp_like(trim(product_id), '^[+-]?[0-9]+$')
                then try_to_number(trim(product_id), 38, 0)
        end as product_id, -- NUMBER(38,0), Snowflake's integer representation
        nullif(trim(product_name), '') as product_name,
        nullif(initcap(lower(trim(category))), '') as category,
        -- Refuse fractional cents / exponent notation instead of silently rounding
        -- (including a tiny negative value rounding to zero and passing tests).
        case
            when regexp_like(trim(price), '^[+-]?([0-9]+([.][0-9]{1,2})?|[.][0-9]{1,2})$')
                then try_to_decimal(trim(price), 18, 2)
        end as price,
        source_filename,
        batch_id,
        ingested_at
    from {{ source('bronze', 'products') }}
)
select
    *,
    -- Demonstration 10% discount of the converted NUMBER(18,2) price (not the
    -- raw Bronze string); price itself is unchanged. Fixed-point multiply, then
    -- round half up to cents. Null when price is null. Widening first keeps
    -- the exact NUMBER(38,4) product for the largest NUMBER(18,2) price.
    -- See docs/products-discounted-price.md.
    -- silver_discounted_price_boundaries reuses the text between the markers.
    -- discounted_price:begin
    cast(round(cast(price as number(38, 2)) * 0.90, 2) as number(18, 2))
    -- discounted_price:end
    as discounted_price
from normalized
