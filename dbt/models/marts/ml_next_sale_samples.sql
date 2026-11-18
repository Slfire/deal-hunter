-- Données du modèle « quand aura lieu la prochaine promo Steam ? » (jeux qui ne sont PAS en promo).
-- Grain : 1 ligne par jeu x date d'observation t.
--
-- Deux sortes de lignes :
--   - entraînement : une date t toutes les {{ var('next_sale_sample_every_days') }} jours sur les
--     {{ var('next_sale_history_years') }} dernières années, pour au plus {{ var('next_sale_max_train_games') }} jeux
--     tirés au hasard (mais toujours les mêmes) ;
--   - à prédire (is_current) : aujourd'hui, pour tous les jeux.
-- Variables : uniquement les promos commencées AVANT t (aucune fuite du futur).
-- Cible : nombre de jours jusqu'au début de la promo suivante (NULL si aucune promo vue depuis),
--   et sa réduction (pour évaluer la règle « même réduction que la dernière fois »).

{% set every = var('next_sale_sample_every_days') %}

with events as (
    select * from {{ ref('stg_steam_price_events') }}
),

episodes as (
    select * from {{ ref('fct_steam_sale_episodes') }}
),

last_day as (
    select max(changed_at)::date as max_date from events
),

games as (
    select itad_id, min(changed_at)::date as first_date
    from events
    group by itad_id
),

train_games as (
    select itad_id
    from games, last_day
    where first_date <= max_date - 60
    order by hash(itad_id)
    limit {{ var('next_sale_max_train_games') }}
),

dates as (
    -- dates alignées sur aujourd'hui (aujourd'hui, -28 j, -56 j…)
    select g.itad_id, (l.max_date - (k * {{ every }})::integer)::date as t, k = 0 as is_current
    from games g
    cross join last_day l
    cross join range(0, ({{ var('next_sale_history_years') }} * 365) // {{ every }} + 1) r(k)
    where l.max_date - (k * {{ every }})::integer >= g.first_date + 60
      and (k = 0 or g.itad_id in (select itad_id from train_games))
),

off_sale as (
    -- on ne prédit la PROCHAINE promo que pour un jeu qui n'est pas déjà en promo à la date t
    select d.*
    from dates d
    where not exists (
        select 1 from episodes e
        where e.itad_id = d.itad_id and e.start_date <= d.t and (e.end_date is null or e.end_date > d.t)
    )
),

price_at_t as (
    select d.itad_id, d.t, ev.regular_cents
    from off_sale d
    asof join events ev on ev.itad_id = d.itad_id and d.t >= ev.changed_at::date
),

past as (
    select
        d.itad_id, d.t,
        count(e.start_date)                                                    as n_sales,
        count(e.start_date) filter (where e.start_date >= d.t - 365)           as n_sales_365,
        count(e.start_date) filter (where e.start_date >= d.t - 730)           as n_sales_730,
        d.t - max(e.start_date)                                                as days_since_sale_start,
        d.t - max(e.end_date)                                                  as days_since_sale_end,
        arg_max(e.cut, e.start_date)                                           as last_cut,
        max(e.cut)                                                             as max_cut_all,
        max(e.cut) filter (where e.start_date >= d.t - 365)                    as max_cut_365,
        avg(e.cut) filter (where e.start_date >= d.t - 730)                    as mean_cut_730,
        avg(e.end_date - e.start_date) filter (where e.start_date >= d.t - 730) as mean_sale_length_730,
        avg((m.start_date is not null)::int) filter (where e.start_date >= d.t - 730) as share_in_major_sales_730
    from off_sale d
    left join episodes e on e.itad_id = d.itad_id and e.start_date < d.t
    left join {{ ref('dim_steam_major_sales') }} m on m.start_date = e.start_date
    group by d.itad_id, d.t
),

gaps as (
    select
        d.itad_id, d.t,
        median(s.gap)                                       as median_gap_days,
        avg(s.gap) filter (where s.start_date >= d.t - 730) as mean_gap_days_730
    from off_sale d
    left join (
        select itad_id, start_date, start_date - lag(start_date) over (partition by itad_id order by start_date) as gap
        from episodes
    ) s on s.itad_id = d.itad_id and s.start_date < d.t
    group by d.itad_id, d.t
),

major as (
    select
        d.itad_id, d.t,
        min(m.start_date) filter (where m.start_date > d.t) - d.t   as days_to_next_major_sale,
        d.t - max(m.start_date) filter (where m.start_date <= d.t)  as days_since_major_sale
    from off_sale d
    left join {{ ref('dim_steam_major_sales') }} m on true      -- left : aucune ligne perdue si la table est vide
    group by d.itad_id, d.t
),

target as (
    select
        d.itad_id, d.t,
        min(e.start_date) - d.t                 as days_to_next_sale,
        arg_min(e.cut, e.start_date)            as next_sale_cut
    from off_sale d
    left join episodes e on e.itad_id = d.itad_id and e.start_date > d.t
    group by d.itad_id, d.t
)

select
    d.itad_id,
    d.t                                         as observed_on,
    d.is_current,
    p.regular_cents,
    ln(1 + p.regular_cents)                     as log_regular_price,
    d.t - g.first_date                          as age_days,
    month(d.t)                                  as month_of_year,
    pa.* exclude (itad_id, t),
    ga.* exclude (itad_id, t),
    m.* exclude (itad_id, t),
    -- la cible n'est connue que si la période qui suit est passée : sinon NULL, et le script ML n'utilise
    -- une ligne pour un horizon H que si observed_on + H <= dernier jour connu
    tg.days_to_next_sale,
    tg.next_sale_cut,
    l.max_date - d.t                            as days_observed_after
from off_sale d
join games g using (itad_id)
join price_at_t p using (itad_id, t)
join past pa using (itad_id, t)
join gaps ga using (itad_id, t)
join major m using (itad_id, t)
join target tg using (itad_id, t)
cross join last_day l
