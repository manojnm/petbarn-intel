"""Deep-scrape field extraction for the seed set.

Source priority for a single product (see `models.provenance.SOURCE_PRIORITY`
and `merge.py`):

    1. GraphQL `custom_attributesV2`   -- authoritative structured EAV data
       (description, ingredients, feeding_guide, benefits, breed, life_stage,
       flavour, nutrition_grade, per-variant price/gtin/stock_status).
    2. JSON-LD on the rendered PDP HTML -- cross-check for offers/gtin/brand;
       also the fallback if the GraphQL call is blocked.
    3. Bazaarvoice (`reviews`/`vendor_summary`)  -- rating/review data, the
       AI "Summary of Reviews" panel.
    4. LLM field-level fallback            -- only if 1-3 all miss a field
       (see `quality` todo; not yet wired in this module).

Recon note (see docs/decisions/002-graphql-attrs-over-html.md): the PDP's
"Ingredients"/"Features & Benefits" accordions are *not* server-rendered --
they hydrate client-side from the same GraphQL data we already fetch here,
so a bespoke HTML/DOM scraper for those fields was unnecessary.
"""
