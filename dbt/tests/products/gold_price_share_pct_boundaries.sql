{{ config(tags=['products']) }}

{% set expression = 'null' -%}
{%- if execute -%}
    {%- set model_code = graph.nodes['model.agent_platform.product_category_summary'].raw_code -%}
    {%- set begin_marker, end_marker = '-- price_share_pct:begin', '-- price_share_pct:end' -%}
    {%- if model_code.count(begin_marker) != 1 or model_code.count(end_marker) != 1 -%}
        {{ exceptions.raise_compiler_error('product_category_summary must contain one price_share_pct:begin/end marker pair.') }}
    {%- endif -%}
    {%- set expression = model_code.split(begin_marker)[1].split(end_marker)[0] -%}
{%- endif %}
-- Runs the Gold model's own price_share_pct expression (the text between its
-- price_share_pct markers) over fixed category totals. Expected values are
-- literals worked out by hand, not recomputed by any rounding expression.
-- depends_on: {{ ref('product_category_summary') }}

with cases as (
    select
        case_name,
        category,
        cast(total_price as number(38, 2)) as total_price,
        cast(expected_price_share_pct as number(5, 2)) as expected_price_share_pct
    from (
        values
            -- Existing five-row sample: 1199.39 and 298.90 of 1498.29.
            ('sample', 'Electronics', 1199.39, 80.05),
            ('sample', 'Furniture', 298.90, 19.95),
            ('single_category', 'Furniture', 298.90, 100.00),
            ('zero_total', 'Electronics', 0.00, 0.00),
            ('zero_total', 'Furniture', 0.00, 0.00),
            -- Just below a half-hundredth: 0.004999999975 and 99.995000000025.
            ('below_half', 'Electronics', 100.00, 0.00),
            ('below_half', 'Furniture', 1999900.01, 100.00),
            -- Exactly at a half-hundredth: 0.005 and 99.995 round up.
            ('at_half', 'Electronics', 0.01, 0.01),
            ('at_half', 'Furniture', 199.99, 100.00),
            -- Just above a half-hundredth: 0.0050002... and 99.9949997...
            ('above_half', 'Electronics', 0.01, 0.01),
            ('above_half', 'Furniture', 199.98, 99.99),
            -- Exactly at a half-hundredth with totals beyond float precision.
            ('large_at_half', 'Electronics', 1000000000000.00, 0.01),
            ('large_at_half', 'Furniture', 19999000000000000.00, 100.00),
            -- Silver's largest NUMBER(18,2) price next to one cent.
            ('large_below_half', 'Electronics', 0.01, 0.00),
            ('large_below_half', 'Furniture', 9999999999999999.99, 100.00)
    ) as boundary (case_name, category, total_price, expected_price_share_pct)
), overall as (
    select
        *,
        sum(total_price) over (partition by case_name) as overall_total_price
    from cases
), actual as (
    select
        *,
        {{ expression }} as price_share_pct
    from overall
)
select *
from actual
where price_share_pct is distinct from expected_price_share_pct
