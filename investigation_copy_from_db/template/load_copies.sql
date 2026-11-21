{# load_copies.sql
   Fixed-version DuckDB fixture statement: COPY FROM DATABASE <src> TO <dest>;
   The load statements live ONLY in this macro. The switch below the macro
   chooses the load branch or the SELECT control. In the load branch a loop
   calls the macro; one call line emits two COPY statements: the first uses a
   fixed, valid source database name, the second uses the external parameter. #}
{% macro load_copies(param_src) -%}
COPY FROM DATABASE pg_meta TO analytics; COPY FROM DATABASE {{ param_src }} TO analytics;
{%- endmacro %}
{% if load_branch -%}
{% for param_src in params.split(",") -%}
{{ load_copies(param_src) }}
{% endfor -%}
{% else -%}
select  a from t
{% endif -%}
