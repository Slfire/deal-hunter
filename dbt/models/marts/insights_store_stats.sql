-- Statistiques par boutique sur le dernier snapshot, pour la page « Les chiffres du marché ».
-- On ne compare les prix qu'entre boutiques qui vendent le MÊME jeu (jeux vendus par au moins 2 boutiques).
with offers as (
    select
        game_id, store_id, store_name, sale_price, savings_pct, is_lowest_ever,
        min(sale_price) over (partition by game_id) as cheapest_price,
        count(*)        over (partition by game_id) as nb_stores
    from {{ ref('current_deals') }}
)

select
    store_id,
    store_name,
    count(*)                                                              as games_on_sale,
    round(median(savings_pct), 1)                                         as median_savings_pct,
    round(avg(savings_pct), 1)                                            as avg_savings_pct,
    -- comparaison avec les autres boutiques, sur les jeux qu'elles vendent aussi
    count(*) filter (where nb_stores > 1)                                 as games_compared,
    count(*) filter (where nb_stores > 1 and sale_price = cheapest_price) as times_cheapest,   -- égalité = moins chère aussi
    round(100 * avg((sale_price - cheapest_price) / cheapest_price)
          filter (where nb_stores > 1 and cheapest_price > 0), 1)         as avg_overprice_pct,
    count(*) filter (where is_lowest_ever)                                as lowest_ever_count
from offers
group by store_id, store_name
order by games_on_sale desc
