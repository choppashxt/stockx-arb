"""schema.org ld+json extraction shared by the non-sneaker retailers.

Handles the two shapes seen in the wild: a bare Product object (apollo.ee) and
a Product nested inside an `@graph` array (klick.ee). A naive top-level `@type`
check finds the first and silently misses the second.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_SCRIPT = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _walk(node: Any, out: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Product" in types:
        out.append(node)
    # @graph holds siblings; other values can nest a Product too
    for key in ("@graph", "mainEntity", "itemListElement"):
        if key in node:
            _walk(node[key], out)


def ld_products(html: str) -> list[dict]:
    """Every schema.org Product object on the page, at any nesting depth."""
    out: list[dict] = []
    for block in _SCRIPT.findall(html or ""):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        _walk(data, out)
    return out


def offer(item: dict) -> dict:
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        return next((o for o in offers if isinstance(o, dict)), {})
    return offers if isinstance(offers, dict) else {}


def gtin_of(item: dict) -> Optional[str]:
    """The barcode, whichever field the shop used. Digits only."""
    for key in ("gtin13", "gtin12", "gtin14", "gtin", "gtin8"):
        raw = item.get(key)
        if raw:
            digits = re.sub(r"\D", "", str(raw))
            if len(digits) >= 8:
                return digits
    return None


def brand_of(item: dict) -> Optional[str]:
    brand = item.get("brand")
    if isinstance(brand, dict):
        return (brand.get("name") or "").strip() or None
    if isinstance(brand, str):
        return brand.strip() or None
    return None


def price_of(item: dict) -> Optional[float]:
    try:
        value = float(str(offer(item).get("price")).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def in_stock(item: dict) -> bool:
    availability = str(offer(item).get("availability") or "")
    return "InStock" in availability or "PreOrder" in availability
