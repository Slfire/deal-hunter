-- Résumés IA des avis Steam : ce que les joueurs aiment / regrettent, en une phrase pour qui est le jeu
select
    cast(app_id as integer)                                              as steam_app_id,
    cast(json_extract_string(payload, '$.summary.aime[*]') as varchar[]) as reviews_likes,
    cast(json_extract_string(payload, '$.summary.regrette[*]') as varchar[]) as reviews_dislikes,
    nullif(trim(payload ->> '$.summary.en_bref'), '')                    as reviews_tldr,
    cast(payload ->> '$.n_reviews' as integer)                           as reviews_count,
    payload ->> '$.model'                                                as reviews_model,
    cast(payload ->> '$.created' as date)                                as reviews_summarized_on
from {{ source('raw', 'review_summaries') }}
where payload ->> '$.summary' is not null
