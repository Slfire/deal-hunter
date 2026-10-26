-- Boutiques, avec les URL complètes des logos
select
    store_id,
    store_name,
    is_active,
    'https://www.cheapshark.com' || logo_path as logo_url,
    'https://www.cheapshark.com' || icon_path as icon_url
from {{ ref('stg_stores') }}
