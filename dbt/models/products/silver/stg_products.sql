-- Preserve every row, including failed conversions and unknown categories.
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
