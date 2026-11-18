-- Identifiant IsThereAnyDeal de chaque jeu Steam (dernière recherche réussie). Grain : 1 ligne par appid.
select
    appid,
    arg_max(itad_id, looked_up_at) as itad_id
from {{ source('raw', 'itad_ids') }}
where itad_id is not null
group by appid
