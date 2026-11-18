-- État actuel de chaque jeu (dernier relevé du journal quotidien). Grain : 1 ligne par jeu ITAD.
select
    itad_id,
    max(captured_at)                                    as captured_at,
    arg_max(steam_price_cents, captured_at)             as steam_price_cents,
    arg_max(steam_regular_cents, captured_at)           as steam_regular_cents,
    arg_max(steam_cut, captured_at)::int                as steam_cut,
    arg_max(best_price_cents, captured_at)              as best_price_cents,
    arg_max(best_cut, captured_at)::int                 as best_cut,
    arg_max(best_shop_id, captured_at)                  as best_shop_id,
    arg_max(low_all_cents, captured_at)                 as low_all_cents
from {{ source('raw', 'itad_prices') }}
group by itad_id
