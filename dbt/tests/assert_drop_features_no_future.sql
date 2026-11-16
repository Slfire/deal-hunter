-- Garde-fou anti-fuite : le prix de référence d'une ligne est bien celui de son propre jour.
-- Si une variable utilisait un jour futur, le meilleur prix vu jusque-là pourrait passer sous le prix du jour
-- d'une manière impossible : ratio_to_min_seen < 1 voudrait dire « plus bas que le minimum déjà vu, jour J compris ».
select game_id, snapshot_date, ratio_to_min_seen, ratio_to_max_seen
from {{ ref('ml_price_drop_features') }}
where ratio_to_min_seen < 0.999 or ratio_to_max_seen > 1.001
