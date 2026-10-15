{% macro generate_schema_name(custom_schema_name, node) -%}
    {# Limit layer routing to this project's products models. In particular,
       connection_check keeps its original target.schema, including PR schemas. #}
    {%- if node.resource_type == 'model'
        and node.package_name == 'agent_platform'
        and 'products' in node.tags
        and custom_schema_name is not none -%}
        {{ return(products_schema(custom_schema_name)) }}
    {%- elif custom_schema_name is none -%}
        {{ return(target.schema) }}
    {%- else -%}
        {{ return(target.schema ~ '_' ~ (custom_schema_name | trim)) }}
    {%- endif -%}
{%- endmacro %}
