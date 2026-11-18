-- Deux promos d'un même jeu ne se chevauchent jamais (sinon le découpage en épisodes est faux)
select a.itad_id, a.start_date, b.start_date as next_start
from {{ ref('fct_steam_sale_episodes') }} a
join {{ ref('fct_steam_sale_episodes') }} b
  on a.itad_id = b.itad_id and b.start_date > a.start_date and b.start_date < coalesce(a.end_date, date '9999-12-31')
