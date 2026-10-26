-- Grain de l'historique : une seule ligne par jeu x boutique x jour
select game_id, store_id, snapshot_date, count(*) as n
from {{ ref('fct_price_history') }}
group by all
having count(*) > 1
