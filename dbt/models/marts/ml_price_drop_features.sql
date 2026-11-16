-- Jeu de données du modèle « ce jeu va-t-il baisser ? ».
-- Grain : 1 ligne par jeu x jour relevé.
--
-- Variables (features) : calculées UNIQUEMENT avec ce qui était connu ce jour-là et avant
--   (fenêtres « preceding », jamais « following ») : sinon le modèle tricherait en regardant le futur.
-- Cible (will_drop) : 1 si, dans les {{ var('drop_horizon_days') }} jours qui suivent, le meilleur prix
--   descend d'au moins {{ (var('drop_threshold') * 100) | int }} % sous le prix du jour.
--   Elle n'existe que quand ces jours sont entièrement passés (label_complete) : pour les jours récents,
--   c'est justement ce qu'on cherche à prédire.
--   Un jour où le jeu n'est plus relevé compte comme « pas de baisse vue » (le plus souvent, la promo est finie).
--   Un jeu gratuit ce jour-là ne peut pas baisser : cible à 0 (et le script ML l'écarte).

{% set horizon = var('drop_horizon_days') %}
{% set threshold = var('drop_threshold') %}

with daily as (
    select * from {{ ref('fct_game_daily_price') }}
),

last_day as (
    select max(snapshot_date) as max_date from daily
),

features as (
    select
        game_id,
        snapshot_date,
        best_price,
        retail_price,
        cheapest_ever_price,
        n_stores,
        n_stores_on_sale,
        steam_on_sale::int                                                  as steam_on_sale,
        1 - best_price / nullif(retail_price, 0)                            as discount,
        best_price / nullif(cheapest_ever_price, 0)                         as ratio_to_lowest_ever,
        (best_price <= cheapest_ever_price * 1.01)::int                     as at_lowest_ever,
        ln(1 + retail_price)                                                as log_retail_price,
        row_number() over w_all - 1                                         as days_seen_before,
        date_diff('day', lag(snapshot_date) over w_all, snapshot_date)      as days_since_prev_seen,
        best_price / nullif(lag(best_price) over w_all, 0)                  as change_vs_prev_seen,
        best_price / nullif(min(best_price) over w_past, 0)                 as ratio_to_min_seen,
        best_price / nullif(max(best_price) over w_past, 0)                 as ratio_to_max_seen,
        count(*) over w_7d                                                  as days_seen_last_7d,
        count(*) filter (where n_stores_on_sale > 0) over w_7d              as days_on_sale_last_7d
    from daily
    window
        w_all  as (partition by game_id order by snapshot_date),
        w_past as (partition by game_id order by snapshot_date rows between unbounded preceding and current row),
        w_7d   as (partition by game_id order by snapshot_date range between interval 6 day preceding and current row)
),

future as (
    -- meilleur prix vu dans les jours suivants (sert uniquement à la cible)
    select
        a.game_id,
        a.snapshot_date,
        min(b.best_price) as min_future_price
    from daily a
    left join daily b
        on  b.game_id = a.game_id
        and b.snapshot_date >  a.snapshot_date
        and b.snapshot_date <= a.snapshot_date + {{ horizon }}
    group by a.game_id, a.snapshot_date
)

select
    f.*,
    f.snapshot_date + {{ horizon }} <= l.max_date                              as label_complete,
    case
        when f.snapshot_date + {{ horizon }} <= l.max_date
        then coalesce(f.best_price > 0 and fu.min_future_price <= f.best_price * (1 - {{ threshold }}), false)::int
    end                                                                       as will_drop,
    f.snapshot_date = l.max_date                                              as is_latest
from features f
join future fu using (game_id, snapshot_date)
cross join last_day l
