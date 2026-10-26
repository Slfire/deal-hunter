-- « Le jeu en bref » : résumé IA de la fiche Steam et des tags joueurs (GitHub Models).
-- Ce n'est PAS un résumé des avis : le site l'étiquette comme résumé de la fiche du jeu.
select
    cast(app_id as integer)                                             as steam_app_id,
    nullif(trim(payload ->> '$.brief.en_bref'), '')                     as brief_summary,
    nullif(trim(payload ->> '$.brief.pour_qui'), '')                    as brief_for_whom,
    cast(json_extract_string(payload, '$.brief.points[*]') as varchar[]) as brief_points,
    payload ->> '$.model'                                               as brief_model,
    cast(payload ->> '$.created' as date)                               as brief_created_on
from {{ source('raw', 'game_briefs') }}
where payload ->> '$.brief' is not null
