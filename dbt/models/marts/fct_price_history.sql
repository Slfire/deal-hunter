-- Historique des prix : 1 ligne par jeu x boutique x jour (dernier relevé du jour).
-- C'est la table qui servira à la prédiction des promos.
select distinct on (game_id, store_id, snapshot_date)
    game_id,
    store_id,
    snapshot_date,
    sale_price,
    normal_price,
    is_on_sale,
    savings_pct,
    deal_rating,
    deal_id
from {{ ref('stg_deals') }}
order by game_id, store_id, snapshot_date, snapshot_ts desc
