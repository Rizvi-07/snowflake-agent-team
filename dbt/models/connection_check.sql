{{ config(materialized='view') }}

select
    1::integer as check_id,
    current_database() as database_name,
    current_warehouse() as warehouse_name