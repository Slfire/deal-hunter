-- Les grandes soldes Steam (été, automne, hiver, printemps…), passées et annoncées.
-- Passées : DÉTECTÉES dans les données, sans liste écrite à la main : un jour où plus de
--   {{ (var('major_sale_share') * 100) | int }} % des jeux suivis démarrent une promo, c'est une grande soldes.
-- À venir : le calendrier officiel de Valve (data/ref/steam_sales_calendar.json).
-- Grain : 1 ligne par jour de début de grande soldes.
with starts as (
    select start_date, count(*) as n_starts
    from {{ ref('fct_steam_sale_episodes') }}
    group by start_date
),

first_seen as (
    select itad_id, min(changed_at)::date as first_date
    from {{ ref('stg_steam_price_events') }}
    group by itad_id
),

detected as (
    select
        s.start_date,
        s.n_starts,
        (select count(*) from first_seen f where f.first_date <= s.start_date) as n_games
    from starts s
),

past as (
    select start_date, 'détectée' as origin, n_starts, n_games, n_starts / n_games as share
    from detected
    where n_games >= 100 and n_starts >= {{ var('major_sale_share') }} * n_games
),

announced as (
    select start_date, 'annoncée' as origin, null::bigint, null::bigint, null::double
    from {{ source('raw', 'steam_sales_calendar') }}
    where start_date > (select coalesce(max(start_date), date '1900-01-01') from past)
)

select * from past
union all
select * from announced
