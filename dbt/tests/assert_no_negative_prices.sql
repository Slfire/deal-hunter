-- Aucun prix négatif dans les deals
select * from {{ ref('stg_deals') }}
where sale_price < 0 or normal_price < 0
