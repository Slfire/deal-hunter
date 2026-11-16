-- Prix de chaque boutique pour chaque jeu suivi, tels que renvoyés par CheapShark /games?ids=.
-- Contrairement à /deals, cette source donne aussi les boutiques où le jeu n'est PAS en promo :
-- c'est elle qui permet de savoir si un jeu a vraiment baissé, toutes boutiques confondues.
-- Grain : 1 ligne par jeu x boutique x jour (dernier relevé du jour).
with offers as (
    select
        cast(game_id as integer)                                       as game_id,
        snapshot_ts,
        cast(snapshot_ts as date)                                      as snapshot_date,
        cast(payload ->> '$.cheapestPriceEver.price' as decimal(10,2)) as cheapest_ever_price,
        unnest(from_json(payload -> '$.deals', '["json"]'))            as offer
    from {{ source('raw', 'games') }}
    where json_type(payload -> '$.deals') = 'ARRAY'
)
select distinct on (game_id, store_id, snapshot_date)
    game_id,
    cast(offer ->> 'storeID' as integer)            as store_id,
    snapshot_date,
    cast(offer ->> 'price' as decimal(10,2))        as price,
    cast(offer ->> 'retailPrice' as decimal(10,2))  as retail_price,
    cheapest_ever_price
from offers
where offer ->> 'price' is not null
order by game_id, store_id, snapshot_date, snapshot_ts desc
