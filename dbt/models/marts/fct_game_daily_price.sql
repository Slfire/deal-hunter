-- Meilleur prix du jour d'un jeu, toutes boutiques confondues.
-- Grain : 1 ligne par jeu x jour où le jeu a été relevé.
-- Un jour sans ligne = le jeu n'était plus dans les promos suivies ce jour-là (on ne connaît pas son prix).
select
    game_id,
    snapshot_date,
    min(price)::double                                   as best_price,
    max(retail_price)::double                            as retail_price,
    -- plus bas historique CheapShark tel qu'il était connu CE jour-là (pas de fuite du futur)
    any_value(cheapest_ever_price)::double               as cheapest_ever_price,
    count(*)                                             as n_stores,
    count(*) filter (where price < retail_price)         as n_stores_on_sale,
    coalesce(bool_or(store_id = 1 and price < retail_price), false) as steam_on_sale
from {{ ref('stg_game_store_prices') }}
group by game_id, snapshot_date
