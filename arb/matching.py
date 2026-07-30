"""Style-code normalization and retail->StockX match scoring.

Correctness is paramount: a wrong match means buying the wrong shoe.
Bias toward "don't alert" — anything below firm confidence goes to the
review queue instead of an alert.
"""
from __future__ import annotations

import re
from typing import Optional

# Tokens that are sizes, not part of a style code.
_SHOE_SIZE = re.compile(r"^\d{1,2}(\.5)?$")            # 4 .. 16, halves
_APPAREL_SIZE = re.compile(r"^(2?X{0,3}S|S|M|L|X{1,3}L|2XL|3XL|4XL|OS|ONE|0)$", re.I)


def normalize_style_code(code: str) -> str:
    """Canonical form for comparisons: uppercase, alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def style_code_candidates_from_sku(sku: str) -> list[str]:
    """Derive manufacturer style-code candidates from a retailer SKU like
    'HQ2010_005_8.5_CNF' (Ballzy: style code + size + configurable marker).

    Returns display-form candidates, most specific first. Ambiguous trailing
    numeric tokens (size vs. color code) yield multiple candidates; the StockX
    exact styleId comparison decides which one is real.
    """
    tokens = [t.strip() for t in re.split(r"[_\s]+", sku.strip()) if t.strip()]
    if tokens and tokens[-1].upper() == "CNF":
        tokens = tokens[:-1]
    if not tokens:
        return []

    candidates: list[list[str]] = []

    def push(toks: list[str]) -> None:
        if toks and toks not in candidates:
            candidates.append(toks)

    rest = list(tokens)
    # strip a waist/length pair (apparel, e.g. ..._30_32)
    if len(rest) >= 3 and rest[-1].isdigit() and rest[-2].isdigit() \
            and len(rest[-1]) == 2 and len(rest[-2]) == 2:
        push(rest[:-2])
    # strip one apparel size
    if len(rest) >= 2 and _APPAREL_SIZE.match(rest[-1]):
        push(rest[:-1])
    # strip one shoe size — and, if the next token is also size-like (rare,
    # ambiguous color codes like '..._49_37.5'), keep both interpretations
    if len(rest) >= 2 and _SHOE_SIZE.match(rest[-1]):
        push(rest[:-1])
        if len(rest) >= 3 and _SHOE_SIZE.match(rest[-2]):
            push(rest[:-2])
    push(rest)  # SKU may carry no size at all

    # some brands' StockX styleId omits a trailing 3-4 digit color code
    # (e.g. Vans VN000EBNBPN1-050 -> VN000EBNBPN1); exact styleId matching
    # keeps the extra candidate safe
    for c in list(candidates):
        if len(c) >= 2 and c[-1].isdigit() and len(c[-1]) in (3, 4):
            push(c[:-1])

    return ["-".join(c) for c in candidates]


def style_ids_match(retail_code: str, stockx_style_id: Optional[str]) -> bool:
    """Exact match against a StockX styleId, tolerant of separator noise.

    StockX sometimes packs several *forms of the same code* into one field
    ('DD1391-100/DD1391 100') — those are fine to match component-wise.
    But multi-item sets ('FB7921-473/FB8002-473', hoodie + joggers) list one
    code PER GARMENT: matching a single garment's code against one component
    would price the part against the whole set's bid (audit 3.1). When the
    components normalize to different codes, only the full combined code
    (i.e. the retailer is also selling the set) may match."""
    if not stockx_style_id:
        return False
    want = normalize_style_code(retail_code)
    if not want:
        return False
    parts = [normalize_style_code(p) for p in re.split(r"[/,]", stockx_style_id)
             if p.strip()]
    parts = [p for p in parts if p]
    if not parts:
        return False
    if len(set(parts)) == 1:            # formatting variants of one code
        return want == parts[0]
    return want == normalize_style_code(stockx_style_id)


def code_in_title(retail_code: str, stockx_title: Optional[str]) -> bool:
    """Does the retailer's product code appear as a whole token in the StockX
    title?

    Non-sneaker products carry an EMPTY `styleId` (verified 2026-07-29 across
    collectibles and watches), so `style_ids_match` can never fire for them.
    The identifying code is in the title instead:

        'LEGO Star Wars Millennium Falcon Ultimate Collector Series Set 75192'
        'Casio G-Shock x Pokemon 30th Anniversary GA-110PKM-7A'

    Search ranking alone is NOT evidence — querying '75192' also returned a
    Gucci jogger (styleId '715192 XJETI 2270'). So the code must be confirmed
    against the title, and only on a token boundary: a bare substring test
    would let '5192' match '715192' and '21044' match '210440'.
    """
    if not stockx_title:
        return False
    want = normalize_style_code(retail_code)
    if len(want) < 4:
        # 3-digit codes collide with years, counts and set-piece numbers far
        # too easily to be treated as identifying on their own.
        return False
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", stockx_title):
        if normalize_style_code(token) == want:
            return True
    return False


# StockX sells region-specific electronics as SEPARATE products whose titles are
# otherwise identical: '... Bundle (US Plug)' vs '... BundIe (EU Plug)'. Buying
# the EU unit against a bid on the US one is the same class of error as a wrong
# shoe size, but invisible in the name.
_REGION_MARKER = re.compile(
    r"\((?:\d{3}-\d{3}V\s*)?(US|EU|UK|AU|JP|KR|CN)\s*(?:Plug|Version|Spec)\)", re.I)


def region_marker(title: Optional[str]) -> Optional[str]:
    """The region marker in a StockX title, e.g. 'US Plug', or None."""
    if not title:
        return None
    m = _REGION_MARKER.search(title)
    return m.group(0).strip("()") if m else None


def region_mismatch(stockx_title: Optional[str],
                    retail_name: Optional[str]) -> Optional[str]:
    """The marker to refuse on, or None when it is safe to proceed.

    Returns the marker whenever StockX pins a region that the retail listing
    does not state. Silence from the retailer is not agreement — an Estonian
    shop selling a EU-plug console rarely says so, which is exactly why this
    has to block rather than assume."""
    marker = region_marker(stockx_title)
    if marker is None:
        return None
    region = re.match(r"(?:\d{3}-\d{3}V\s*)?([A-Z]{2})", marker, re.I)
    if region and retail_name:
        want = region.group(1).upper()
        if re.search(rf"\(\s*{want}\b", retail_name, re.I) or \
                re.search(rf"\b{want}\s*(?:Plug|Version|Spec)\b", retail_name, re.I):
            return None      # retailer states the same region — safe
    return marker


def size_match_confidence(retail_us: Optional[str], retail_eu: Optional[str],
                          variant_us: Optional[str],
                          variant_eu: Optional[str]) -> float:
    """How sure are we the retail size row is this StockX variant?

    1.0  = US sizes agree and the EU conversion doesn't contradict, OR the
           retailer only gives EU and it equals StockX's own EU conversion for
           this variant (the style code already pinned the exact product incl.
           gender, so StockX's own size chart is authoritative, not a guess —
           the caller must still reject the match if several variants share
           the EU label)
    0.0  = no safe mapping — never guess a size."""
    if retail_us and variant_us:
        if not _sizes_equal(retail_us, variant_us):
            return 0.0
        if retail_eu and variant_eu and not _sizes_equal(retail_eu, variant_eu):
            return 0.5     # US matches but EU disagrees -> review, don't alert
        return 1.0
    if retail_eu and variant_eu:
        return 1.0 if _sizes_equal(retail_eu, variant_eu) else 0.0
    return 0.0


_LETTER_SIZES = {
    "XXS": "XXS", "2XS": "XXS",
    "XS": "XS",
    "S": "S", "SMALL": "S",
    "M": "M", "MEDIUM": "M",
    "L": "L", "LARGE": "L",
    "XL": "XL", "1X": "XL",
    "XXL": "XXL", "2XL": "XXL", "2X": "XXL",
    "XXXL": "XXXL", "3XL": "XXXL", "3X": "XXXL",
    "OS": "OS", "ONE SIZE": "OS", "OSFA": "OS",
}


def _letter(size: str) -> Optional[str]:
    """Normalize an apparel size, or None if it isn't one."""
    key = re.sub(r"[^A-Z0-9 ]", "", size.strip().upper())
    return _LETTER_SIZES.get(key)


def _sizes_equal(a: str, b: str) -> bool:
    """Do these two size labels denote the same size?

    The apparel-letter check runs FIRST: _num() grabs the leading digit of
    "2XL", so a numeric-first order rated 2XL == 2XS == "2" at full confidence
    (audit 10.1). If either label is a recognised apparel size, both must be,
    and they must normalize to the same letter ("Large" == "L", but L != M —
    treating any-letter-matches-any-letter as equal would sell an S into an
    XL bid). Only then do numeric sizes compare numerically ("EU 44" == "44").
    Anything unrecognised returns False — never guess a size.
    """
    la, lb = _letter(a), _letter(b)
    if la is not None or lb is not None:
        return la is not None and la == lb   # one apparel, one not: no match
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return False


# adidas sizes come as "EU 44 2/3" / "42 1/3". Without this, the plain number
# regex reads "44 2/3" as 44 and happily matches it to EU 44 — a different
# shoe size, and a different pair in the box.
_FRACTION_RE = re.compile(r"(\d+)\s+(\d)\s*/\s*(\d)")
_UNICODE_FRACTIONS = {"⅓": " 1/3", "⅔": " 2/3", "½": " 1/2",
                      "¼": " 1/4", "¾": " 3/4"}


def _num(size: str) -> Optional[float]:
    s = size.replace(",", ".")
    for glyph, ascii_form in _UNICODE_FRACTIONS.items():
        s = s.replace(glyph, ascii_form)
    if (m := _FRACTION_RE.search(s)):
        whole, num, den = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return whole + num / den if den else whole
    m = re.search(r"\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None
