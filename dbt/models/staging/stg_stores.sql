-- Boutiques : dernière version connue de chaque boutique
select distinct on (store_id)
    cast(storeID as integer)                    as store_id,
    trim(storeName)                             as store_name,
    isActive = '1'                              as is_active,
    json_extract_string(images, '$.logo')       as logo_path,
    json_extract_string(images, '$.icon')       as icon_path,
    snapshot_ts
from {{ source('raw', 'stores') }}
order by store_id, snapshot_ts desc
