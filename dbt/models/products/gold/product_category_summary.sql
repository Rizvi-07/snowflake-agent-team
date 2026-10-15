select
    category,
    count(*) as product_count,
    sum(price) as total_price,
    cast(avg(price) as number(38, 6)) as average_price,
    min(price) as minimum_price,
    max(price) as maximum_price
from {{ ref('stg_products') }}
group by category
