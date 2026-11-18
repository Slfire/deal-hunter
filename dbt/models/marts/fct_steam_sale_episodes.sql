-- Les promos Steam de chaque jeu, une ligne par promo (« épisode »).
-- Un épisode = une période continue où la réduction Steam est > 0. Sa réduction = la plus forte de la période.
-- Grain : 1 ligne par jeu x promo.
with events as (
    select
        *,
        lead(changed_at) over w                                               as valid_to,
        ((cut > 0) is distinct from (lag(cut) over w > 0))::int               as state_changed
    from {{ ref('stg_steam_price_events') }}
    window w as (partition by itad_id order by changed_at)
),

numbered as (
    select *, sum(state_changed) over (partition by itad_id order by changed_at rows unbounded preceding) as period_id
    from events
)

select
    itad_id,
    period_id,
    min(changed_at)::date                                as start_date,
    -- fin = premier changement qui ramène la réduction à 0 ; NULL si la promo est en cours
    case when bool_or(valid_to is null) then null else max(valid_to)::date end as end_date,
    max(cut)                                             as cut,
    arg_min(regular_cents, changed_at)                   as regular_cents,
    min(price_cents)                                     as min_price_cents
from numbered
where cut > 0
group by itad_id, period_id
