-- Catalogue des jeux Steam payants (SteamSpy). Grain : 1 ligne par appid.
select distinct on (appid)
    appid,
    name,
    developer,
    publisher,
    owners_min,
    owners_max,
    reviews,
    positive,
    negative,
    initial_price_usd_cents
from {{ source('raw', 'steam_catalog') }}
where appid is not null and name <> ''
order by appid, owners_min desc
