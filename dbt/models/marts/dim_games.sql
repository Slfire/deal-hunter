-- Un jeu = une ligne : dernière info connue + plus bas historique + fiche Steam + tags + prix en euros
with latest as (
    select distinct on (game_id)
        game_id, title, internal_name, steam_app_id, release_date,
        metacritic_score, steam_rating_pct, steam_rating_count, steam_rating_text,
        metacritic_link, thumb_url,
        snapshot_ts as last_seen_ts
    from {{ ref('stg_deals') }}
    order by game_id, snapshot_ts desc
),

our_low as (   -- notre propre relevé, au cas où CheapShark n'aurait pas l'info
    select distinct on (game_id)
        game_id,
        sale_price    as our_lowest_price,
        snapshot_date as our_lowest_date
    from {{ ref('fct_price_history') }}
    order by game_id, sale_price, snapshot_date desc
),

tracked as (
    select game_id, count(distinct snapshot_date) as days_tracked
    from {{ ref('fct_price_history') }}
    group by game_id
)

select
    l.*,
    t.days_tracked,
    -- plus bas prix historique : le plus bas entre CheapShark (tout l'historique) et nos relevés
    case when i.cheapest_ever_price is null or o.our_lowest_price < i.cheapest_ever_price
         then o.our_lowest_price else i.cheapest_ever_price end   as lowest_ever_price,
    case when i.cheapest_ever_price is null or o.our_lowest_price < i.cheapest_ever_price
         then o.our_lowest_date else i.cheapest_ever_date end     as lowest_ever_date,
    i.cheapest_ever_price is not null                             as has_full_history,
    s.description,
    s.header_image,
    s.genres,
    s.developers,
    s.has_coop,
    s.has_multi,
    s.is_free,
    coalesce(g.tags, []::varchar[])                               as tags,
    g.owners_range,
    p.steam_price_eur,
    p.steam_normal_eur,
    p.steam_discount_pct
from latest l
left join tracked t using (game_id)
left join our_low o using (game_id)
left join {{ ref('stg_games_info') }} i using (game_id)
left join {{ ref('stg_steam') }} s using (steam_app_id)
left join {{ ref('stg_steamspy_tags') }} g using (steam_app_id)
left join {{ ref('stg_steam_prices') }} p using (steam_app_id)
