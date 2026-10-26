-- Le plus bas historique ne peut pas être plus cher que le meilleur prix du jour
select g.game_id, g.lowest_ever_price, min(d.sale_price) as best_today
from {{ ref('dim_games') }} g
join {{ ref('current_deals') }} d using (game_id)
group by all
having g.lowest_ever_price > min(d.sale_price)
