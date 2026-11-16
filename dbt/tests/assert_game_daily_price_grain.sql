-- Un seul meilleur prix par jeu et par jour
select game_id, snapshot_date, count(*) as n
from {{ ref('fct_game_daily_price') }}
group by game_id, snapshot_date
having count(*) > 1
