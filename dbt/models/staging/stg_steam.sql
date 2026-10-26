-- Fiches Steam : on ne garde que les jeux trouvés (success = true)
-- ids de catégories Steam (indépendants de la langue) :
--   1 multijoueur, 9 coop, 36 PvP en ligne, 38 coop en ligne, 39 coop écran partagé, 48 coop LAN, 49 PvP
with s as (
    select
        cast(app_id as integer)                                                     as steam_app_id,
        payload -> '$.data'                                                         as d,
        cast(json_extract_string(payload, '$.data.categories[*].id') as integer[]) as cat_ids
    from {{ source('raw', 'steam') }}
    where cast(payload ->> '$.success' as boolean)
)
select
    steam_app_id,
    d ->> '$.type'                                      as steam_type,
    d ->> '$.short_description'                         as description,
    d ->> '$.header_image'                              as header_image,
    json_extract_string(d, '$.genres[*].description')   as genres,
    json_extract_string(d, '$.developers[*]')           as developers,
    list_has_any(cat_ids, [9, 38, 39, 48])              as has_coop,
    list_has_any(cat_ids, [1, 36, 49, 9, 38, 39, 48])   as has_multi,
    cast(d ->> '$.is_free' as boolean)                  as is_free
from s
