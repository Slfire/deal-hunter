-- La cible n'existe que si les jours à prédire sont entièrement passés :
-- une cible sur une fenêtre incomplète ferait apprendre « pas de baisse » à tort.
select game_id, snapshot_date, label_complete, will_drop
from {{ ref('ml_price_drop_features') }}
where (label_complete and will_drop is null)
   or (not label_complete and will_drop is not null)
