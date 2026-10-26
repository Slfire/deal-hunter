-- Les deals du dernier snapshot, enrichis, prêts pour le site
with last_snap as (
    select max(snapshot_ts) as ts from {{ ref('stg_deals') }}
)

select
    d.game_id,
    d.title,
    s.store_name,
    d.store_id,
    d.sale_price,
    d.normal_price,
    d.savings_pct,
    d.deal_rating,
    d.sale_price <= g.lowest_ever_price                          as is_lowest_ever,
    'https://www.cheapshark.com/redirect?dealID=' || d.deal_id   as deal_url,
    d.thumb_url,
    d.snapshot_ts
from {{ ref('stg_deals') }} d
join last_snap l on d.snapshot_ts = l.ts
left join {{ ref('dim_stores') }} s using (store_id)
left join {{ ref('dim_games') }}  g using (game_id)
