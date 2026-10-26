-- Tags utilisateurs SteamSpy, déjà triés par nombre de votes : on garde les 12 premiers.
-- Quand un jeu n'a aucun tag, SteamSpy renvoie un tableau vide [] au lieu d'un objet.
select
    cast(app_id as integer)                                              as steam_app_id,
    case when json_type(payload -> '$.tags') = 'OBJECT'
         then list_slice(json_keys(payload -> '$.tags'), 1, 12)
         else []::varchar[] end                                          as tags,
    nullif(cast(payload ->> '$.positive' as integer), 0)                 as spy_positive,
    nullif(cast(payload ->> '$.negative' as integer), 0)                 as spy_negative,
    payload ->> '$.owners'                                               as owners_range
from {{ source('raw', 'steamspy') }}
where payload ->> '$.appid' is not null
