{{ config(tags=['products']) }}

{% set expression = 'null' -%}
{%- if execute -%}
    {%- set model_code = graph.nodes['model.agent_platform.stg_products'].raw_code -%}
    {%- set begin_marker, end_marker = '-- discounted_price:begin', '-- discounted_price:end' -%}
    {%- if model_code.count(begin_marker) != 1 or model_code.count(end_marker) != 1 -%}
        {{ exceptions.raise_compiler_error('stg_products must contain one discounted_price:begin/end marker pair.') }}
    {%- endif -%}
    {%- set expression = model_code.split(begin_marker)[1].split(end_marker)[0] -%}
{%- endif %}
-- Runs the Silver model's own discounted_price expression (the text between
-- its discounted_price markers) over fixed prices. Expected values are
-- literals worked out by hand, not recomputed by any rounding expression.
-- depends_on: {{ ref('stg_products') }}

with cases as (
    select
        case_name,
        cast(price as number(18, 2)) as price,
        cast(expected_discounted_price as number(18, 2)) as expected_discounted_price
    from (
        values
            ('hundred', 100.00, 90.00),
            ('sample_mouse', 49.90, 44.91),
            -- 9.045 is exactly half a cent and rounds up.
            ('half_cent', 10.05, 9.05),
            ('zero', 0.00, 0.00),
            -- 0.009 rounds up to the smallest positive cent.
            ('one_cent', 0.01, 0.01),
            -- 0.045 is exactly half a cent and rounds up.
            ('five_cents', 0.05, 0.05),
            -- 0.054 is below half a cent and rounds down.
            ('six_cents', 0.06, 0.05),
            -- 899.991 rounds down.
            ('sample_laptop', 899.99, 809.99),
            -- Silver's largest NUMBER(18,2) price: 8999999999999999.991.
            ('largest', 9999999999999999.99, 8999999999999999.99)
    ) as boundary (case_name, price, expected_discounted_price)
), actual as (
    select
        *,
        {{ expression }} as discounted_price
    from cases
)
select *
from actual
where discounted_price is distinct from expected_discounted_price
