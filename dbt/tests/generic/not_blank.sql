{% test not_blank(model, column_name) %}
select *
from {{ model }}
where trim({{ column_name }}) = ''
{% endtest %}
