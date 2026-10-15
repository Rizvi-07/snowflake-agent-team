{% macro products_schema(layer) -%}
    {%- set layer = layer | trim | upper -%}
    {%- if layer not in ['BRONZE', 'SILVER', 'GOLD'] -%}
        {{ exceptions.raise_compiler_error('Unknown products layer: ' ~ layer) }}
    {%- endif -%}
    {%- set schema = target.schema | trim | upper -%}
    {%- if schema in ['SILVER', 'BRONZE'] -%}
        {{ return(layer) }}
    {%- endif -%}
    {%- set pr = modules.re.fullmatch('PR_([1-9][0-9]*)_(SILVER|BRONZE)', schema) -%}
    {%- if pr -%}
        {%- set result = 'PR_' ~ pr.group(1) ~ '_' ~ layer -%}
        {%- if result | length > 255 -%}
            {{ exceptions.raise_compiler_error('Products PR schema exceeds 255 characters.') }}
        {%- endif -%}
        {{ return(result) }}
    {%- endif -%}
    {{ exceptions.raise_compiler_error(
        'Products require target.schema SILVER, BRONZE, PR_<number>_SILVER or PR_<number>_BRONZE.'
    ) }}
{%- endmacro %}
