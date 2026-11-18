-- Chaque changement de prix Steam France d'un jeu, de sa sortie à aujourd'hui.
-- Deux sources mises bout à bout :
--   1. l'historique IsThereAnyDeal récupéré une fois par jeu (rattrapage) ;
--   2. notre journal quotidien (itad_prices), pour tout ce qui s'est passé APRÈS ce rattrapage.
-- Grain : 1 ligne par jeu x changement.
with backfill as (
    select distinct itad_id, changed_at, price_cents, regular_cents, cut::int as cut
    from {{ source('raw', 'itad_history') }}
),

backfill_end as (
    select itad_id, max(changed_at) as last_backfill_at from backfill group by itad_id
),

daily as (
    select p.itad_id, p.captured_at as changed_at, p.steam_price_cents as price_cents,
           p.steam_regular_cents as regular_cents, p.steam_cut::int as cut
    from {{ source('raw', 'itad_prices') }} p
    join backfill_end b using (itad_id)
    where p.captured_at > b.last_backfill_at and p.steam_price_cents is not null
),

unioned as (
    select * from backfill
    union all
    select * from daily
)

-- le journal quotidien peut répéter un état déjà connu (il note aussi les changements d'autres boutiques) :
-- on ne garde une ligne que si le prix Steam a vraiment changé
select itad_id, changed_at, price_cents, regular_cents, cut
from (
    select *, lag(price_cents) over w as prev_price, lag(regular_cents) over w as prev_regular, lag(cut) over w as prev_cut
    from unioned
    window w as (partition by itad_id order by changed_at)
)
where prev_price is null
   or price_cents is distinct from prev_price
   or regular_cents is distinct from prev_regular
   or cut is distinct from prev_cut
