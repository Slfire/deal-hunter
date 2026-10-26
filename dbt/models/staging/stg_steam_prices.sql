-- Prix Steam en euros : dernier relevé par jeu.
-- Les jeux gratuits ou sans prix renvoient "data": [] -> pas de price_overview, on les écarte.
select distinct on (steam_app_id)
    cast(app_id as integer)                                                    as steam_app_id,
    payload ->> '$.data.price_overview.currency'                               as currency,
    cast(payload ->> '$.data.price_overview.final'   as integer) / 100.0       as steam_price_eur,
    cast(payload ->> '$.data.price_overview.initial' as integer) / 100.0       as steam_normal_eur,
    cast(payload ->> '$.data.price_overview.discount_percent' as integer)      as steam_discount_pct,
    snapshot_ts
from {{ source('raw', 'steam_prices') }}
where cast(payload ->> '$.success' as boolean)
  and payload ->> '$.data.price_overview.final' is not null
order by cast(app_id as integer), snapshot_ts desc
