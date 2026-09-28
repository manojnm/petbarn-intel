"""Primary deep-scrape source: Petbarn's public Magento GraphQL endpoint,
queried per product by `url_key` for the full `custom_attributesV2` EAV
payload plus per-variant (per-size) data.

Verified live (2026-09-28) against a real seed product: `ingredients`,
`feeding_guide`, and `benefits` are populated custom attributes here -- the
PDP's accordions for those sections are *not* server-rendered HTML (see
`docs/decisions/002-graphql-attrs-over-html.md`), so this is both the
highest-priority *and* effectively the only reliable source for them.
"""

from __future__ import annotations

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.extract.text_utils import (
    clean_text,
    html_bullets_to_list,
    parse_float,
)
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

GRAPHQL_URL = "https://www.petbarn.com.au/graphql"
IMAGE_BASE = "https://www.petbarn.com.au/media/catalog/product"

_ATTR_FIELDS = (
    "code "
    "... on AttributeValue { value } "
    "... on AttributeSelectedOptions { selected_options { label value } }"
)

PRODUCT_DETAIL_QUERY = f"""
query ProductDetail($urlKey: String!) {{
  products(filter: {{ url_key: {{ eq: $urlKey }} }}) {{
    items {{
      sku
      name
      url_key
      stock_status
      price_range {{
        minimum_price {{ regular_price {{ value }} final_price {{ value }} }}
        maximum_price {{ regular_price {{ value }} final_price {{ value }} }}
      }}
      custom_attributesV2 {{ items {{ {_ATTR_FIELDS} }} }}
      ... on ConfigurableProduct {{
        configurable_options {{ attribute_code label values {{ label value_index }} }}
        variants {{
          product {{
            sku
            stock_status
            gtin
            price_range {{
              minimum_price {{ regular_price {{ value }} final_price {{ value }} }}
            }}
            custom_attributesV2 {{ items {{ {_ATTR_FIELDS} }} }}
          }}
          attributes {{ code value_index }}
        }}
      }}
    }}
  }}
}}
"""

# Custom-attribute codes that map onto Product.specification (label-based
# EAV selects -- breed/life-stage/etc). We drop the generic "All ..."
# placeholder option that Magento includes in every multiselect.
_SPEC_ATTRS = {
    "breed": "breed",
    "life_stage": "life_stage",
    "flavour": "flavour",
    "nutrition_grade": "nutrition_grade",
    "nutritional_option": "nutritional_option",
    "nutrition_type": "nutrition_type",
    "pet_type": "pet_type",
    "weight_control": "weight_control",
}


def _attrs_to_map(items: list[dict]) -> dict:
    """`custom_attributesV2.items` -> {code: str value | list[{"label","value"}]}"""
    out: dict = {}
    for it in items or []:
        code = it.get("code")
        if not code:
            continue
        if "value" in it and it["value"] is not None:
            out[code] = it["value"]
        elif "selected_options" in it:
            out[code] = it["selected_options"] or []
    return out


def _option_labels(attrs: dict, code: str) -> list[str]:
    opts = attrs.get(code) or []
    if not isinstance(opts, list):
        return []
    return [o["label"] for o in opts if o.get("label") and not o["label"].startswith("All ")]


async def fetch_product_detail(fetcher: TieredFetcher, url_key: str) -> dict | None:
    """Fetch and parse one product's full GraphQL detail. Returns None if the
    product isn't found (e.g. discontinued since the census ran) or the
    request is blocked after tier escalation.
    """
    result = await fetcher.fetch(
        GRAPHQL_URL,
        resource_type="product_page",
        json_body={"query": PRODUCT_DETAIL_QUERY, "variables": {"urlKey": url_key}},
        respect_robots=False,
    )
    if not result.ok():
        logger.warning("product_detail_fetch_failed", url_key=url_key, reason=result.block_reason)
        return None
    import orjson

    payload = orjson.loads(result.content)
    if payload.get("errors"):
        logger.warning("product_detail_graphql_errors", url_key=url_key, errors=payload["errors"])
    items = payload.get("data", {}).get("products", {}).get("items", [])
    if not items:
        return None
    return parse_product_detail(items[0])


def parse_product_detail(item: dict) -> dict:
    """Normalize one GraphQL `products.items[]` entry into the flat field
    dict `merge.py` consumes. Every returned field is implicitly sourced
    from `SourceKind.GRAPHQL` -- the caller stamps provenance.
    """
    attrs = _attrs_to_map(item.get("custom_attributesV2", {}).get("items", []))
    price_range = item.get("price_range") or {}
    min_price = parse_float(
        (price_range.get("minimum_price") or {}).get("final_price", {}).get("value")
    )
    max_price = parse_float(
        (price_range.get("maximum_price") or {}).get("final_price", {}).get("value")
    )

    specification: dict[str, str] = {}
    for attr_code, spec_key in _SPEC_ATTRS.items():
        labels = _option_labels(attrs, attr_code)
        if labels:
            specification[spec_key] = ", ".join(labels)
    if attrs.get("internal_type"):
        specification["product_type"] = clean_text(attrs["internal_type"])

    variants = []
    size_by_index: dict[str, str] = {}
    for opt in item.get("configurable_options") or []:
        if opt.get("attribute_code") == "size":
            for v in opt.get("values", []):
                size_by_index[str(v["value_index"])] = v["label"]

    for v in item.get("variants") or []:
        vp = v.get("product") or {}
        vattrs = _attrs_to_map(vp.get("custom_attributesV2", {}).get("items", []))
        size_label = None
        for a in v.get("attributes") or []:
            if str(a.get("value_index")) in size_by_index:
                size_label = size_by_index[str(a["value_index"])]
        v_price_range = vp.get("price_range") or {}
        v_price = parse_float(
            (v_price_range.get("minimum_price") or {}).get("final_price", {}).get("value")
        ) or parse_float(vattrs.get("price"))
        image_path = vattrs.get("image") or attrs.get("image")
        variants.append(
            {
                "sku": vp.get("sku"),
                "name": clean_text(vattrs.get("name")) or item.get("name"),
                "size": size_label,
                "gtin": vp.get("gtin") or None,
                "price": v_price,
                "member_price": parse_float(vattrs.get("member_price")),
                "availability": vp.get("stock_status"),
                "image_url": f"{IMAGE_BASE}{image_path}" if image_path else None,
                "url": None,  # per-variant url_key not requested; base PDP url covers it
            }
        )

    if not variants:
        # Simple (non-configurable) product: exactly one "variant" == itself.
        image_path = attrs.get("image")
        variants.append(
            {
                "sku": item.get("sku"),
                "name": item.get("name"),
                "size": None,
                "gtin": None,
                "price": min_price,
                "member_price": parse_float(attrs.get("member_price")),
                "availability": item.get("stock_status"),
                "image_url": f"{IMAGE_BASE}{image_path}" if image_path else None,
                "url": None,
            }
        )

    return {
        "sku": item.get("sku"),
        "name": item.get("name"),
        "stock_status": item.get("stock_status"),
        "description": clean_text(attrs.get("description")),
        "short_description": clean_text(attrs.get("short_description")),
        "ingredients": clean_text(attrs.get("ingredients")),
        "feeding_guide": clean_text(attrs.get("feeding_guide")),
        "features": html_bullets_to_list(attrs.get("benefits")),
        "specification": specification,
        "gtin": clean_text(attrs.get("gtin")) or clean_text(attrs.get("barcode")),
        "price_min": min_price,
        "price_max": max_price,
        "variants": variants,
    }
