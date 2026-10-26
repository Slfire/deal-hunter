-- Plus bas prix historique selon CheapShark (dernière version connue par jeu)
select distinct on (game_id)
    cast(game_id as integer)                                                  as game_id,
    cast(payload ->> '$.cheapestPriceEver.price' as decimal(10,2))            as cheapest_ever_price,
    case when cast(payload ->> '$.cheapestPriceEver.date' as bigint) > 0
         then cast(to_timestamp(cast(payload ->> '$.cheapestPriceEver.date' as bigint)) as date)
    end                                                                       as cheapest_ever_date,
    snapshot_ts
from {{ source('raw', 'games') }}
where payload ->> '$.cheapestPriceEver.price' is not null
order by cast(game_id as integer), snapshot_ts desc
