{{ config(tags=['products']) }}

with counts as (
    select
        (select count(*) from {{ source('bronze', 'products') }}) as bronze_count,
        (select count(*) from {{ ref('stg_products') }}) as silver_count
)
select * from counts
where bronze_count <> silver_count
