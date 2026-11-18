-- Garde-fou anti-fuite : aucune variable ne doit dépendre d'une promo commencée après la date d'observation.
-- Une promo « depuis » qui serait négative voudrait dire qu'on regarde le futur.
select itad_id, observed_on, days_since_sale_start, days_since_sale_end, days_to_next_sale
from {{ ref('ml_next_sale_samples') }}
where days_since_sale_start <= 0 or days_since_sale_end < 0 or days_to_next_sale <= 0
