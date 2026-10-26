-- Deals typés et nettoyés : un deal = l'offre d'une boutique pour un jeu, à un instant donné
select
    snapshot_ts,
    cast(snapshot_ts as date)                                       as snapshot_date,
    dealID                                                          as deal_id,
    cast(gameID as integer)                                         as game_id,
    cast(storeID as integer)                                        as store_id,
    trim(title)                                                     as title,
    internalName                                                    as internal_name,
    cast(salePrice   as decimal(10,2))                              as sale_price,
    cast(normalPrice as decimal(10,2))                              as normal_price,
    isOnSale = '1'                                                  as is_on_sale,
    round(cast(savings as double), 2)                               as savings_pct,
    -- faux zéros : 0 = « pas de note », pas une vraie note
    nullif(cast(metacriticScore as integer), 0)                     as metacritic_score,
    case when cast(steamRatingCount as integer) > 0
         then cast(steamRatingPercent as integer) end               as steam_rating_pct,
    nullif(cast(steamRatingCount as integer), 0)                    as steam_rating_count,
    steamRatingText                                                 as steam_rating_text,
    cast(steamAppID as integer)                                     as steam_app_id,
    case when releaseDate > 0 then cast(to_timestamp(releaseDate) as date) end      as release_date,
    case when lastChange  > 0 then cast(to_timestamp(lastChange) as timestamp) end  as last_change_ts,
    cast(dealRating as double)                                      as deal_rating,
    metacriticLink                                                  as metacritic_link,
    thumb                                                           as thumb_url,
    source_file
from {{ source('raw', 'deals') }}
-- un même deal peut apparaître sur 2 pages si l'API bouge pendant la pagination
qualify row_number() over (partition by snapshot_ts, dealID order by source_file) = 1
