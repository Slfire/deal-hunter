{# Utilise le schéma tel quel (staging, marts) au lieu du préfixe par défaut de dbt (main_staging). #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name | trim if custom_schema_name else target.schema }}
{%- endmacro %}
