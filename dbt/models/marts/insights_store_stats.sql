-- Statistiques par boutique sur le dernier snapshot, pour la page Insights :
-- combien de jeux en promo, réduction moyenne, et combien de fois la boutique est la moins chère.
with offers as (
    select
        game_id, store_id, store_name, sale_price, savings_pct, is_lowest_ever,
        rank() over (partition by game_id order by sale_price) as price_rank,
        count(*) over (partition by game_id)                   as nb_stores
    from {{ ref('current_deals') }}
)

select
    store_id,
    store_name,
    count(*)                                                         as games_on_sale,
    round(avg(savings_pct), 1)                                       as avg_savings_pct,
    count(*) filter (where price_rank = 1 and nb_stores > 1)         as times_cheapest,
    count(*) filter (where nb_stores > 1)                            as games_compared,
    count(*) filter (where is_lowest_ever)                           as lowest_ever_count
from offers
group by store_id, store_name
order by games_on_sale desc
