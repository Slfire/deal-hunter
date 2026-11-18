-- Le catalogue des jeux Steam payants, enrichi : prix du jour, historique des promos, réduction attendue.
-- Grain : 1 ligne par jeu (appid Steam).
--
-- « Réduction attendue » de la prochaine promo : la réduction de la DERNIÈRE promo, avec comme fourchette
-- la plus faible et la plus forte des 3 dernières. Une règle simple, gardée parce qu'elle fait mieux qu'un
-- modèle de machine learning testé sur les mêmes données (voir README) : la plupart des jeux répètent
-- exactement leur dernière réduction.
with episodes as (
    select * from {{ ref('fct_steam_sale_episodes') }}
),

sales as (
    select
        itad_id,
        count(*)                                                         as n_sales,
        count(*) filter (where start_date >= current_date - 365)         as n_sales_last_year,
        max(start_date)                                                  as last_sale_start,
        arg_max(cut, start_date)                                         as last_cut,
        max(cut)                                                         as max_cut,
        list(cut order by start_date desc)[1:3]                          as last_3_cuts,
        list(start_date order by start_date desc)[1:12]                  as recent_sale_starts
    from episodes
    group by itad_id
),

history_span as (
    select itad_id, min(changed_at)::date as first_price_date, max(changed_at)::date as last_price_change
    from {{ ref('stg_steam_price_events') }}
    group by itad_id
),

current_state as (
    -- prix du jour : journal quotidien s'il existe, sinon dernier changement connu de l'historique
    select
        ev.itad_id,
        coalesce(cp.steam_price_cents, ev.price_cents)       as steam_price_cents,
        coalesce(cp.steam_regular_cents, ev.regular_cents)   as steam_regular_cents,
        coalesce(cp.steam_cut, ev.cut)                       as steam_cut,
        cp.best_price_cents, cp.best_cut, cp.best_shop_id, cp.low_all_cents,
        cp.captured_at
    from (select itad_id, arg_max(price_cents, changed_at) as price_cents, arg_max(regular_cents, changed_at) as regular_cents,
                 arg_max(cut, changed_at) as cut
          from {{ ref('stg_steam_price_events') }} group by itad_id) ev
    left join {{ ref('stg_itad_current_prices') }} cp using (itad_id)
)

select
    c.appid,
    i.itad_id,
    c.name,
    c.developer,
    c.publisher,
    c.owners_min,
    c.owners_max,
    c.reviews,
    case when c.reviews > 0 then round(100.0 * c.positive / c.reviews) end      as positive_pct,
    cs.steam_price_cents,
    cs.steam_regular_cents,
    coalesce(cs.steam_cut, 0)                                                   as steam_cut,
    coalesce(cs.steam_cut, 0) > 0                                               as on_sale_now,
    cs.best_price_cents,
    cs.best_cut,
    cs.best_shop_id,
    cs.low_all_cents,
    coalesce(s.n_sales, 0)                                                      as n_sales,
    coalesce(s.n_sales_last_year, 0)                                            as n_sales_last_year,
    s.last_sale_start,
    s.last_cut                                                                  as expected_cut,
    list_min(s.last_3_cuts)                                                     as expected_cut_low,
    list_max(s.last_3_cuts)                                                     as expected_cut_high,
    s.max_cut,
    s.recent_sale_starts,
    h.first_price_date,
    h.last_price_change
from {{ ref('stg_steam_catalog') }} c
left join {{ ref('stg_itad_ids') }} i using (appid)
left join sales s using (itad_id)
left join history_span h using (itad_id)
left join current_state cs using (itad_id)
