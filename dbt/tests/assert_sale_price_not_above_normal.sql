-- Un prix promo ne devrait jamais dépasser le prix normal
select * from {{ ref('stg_deals') }}
where sale_price > normal_price
