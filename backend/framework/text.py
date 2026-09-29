"""Small text-formatting primitives shared across the generators.

Why this module exists
----------------------
A decision point's ``备选方案`` (alternatives) line is rendered in two
places: :mod:`prd_generator` writes it into the PRD markdown, and
:mod:`prd_review` renders it again for the review surface the operator
reads. Both hand-rolled the same expression::

    '；'.join(f'① {a}' for a in alts)

— with the bullet **hard-coded** instead of taken from the index. A
decision point with two alternatives therefore rendered as
``① 方案甲；① 方案乙``: two mutually exclusive options that look like
the same option, and a reader cannot tell them apart.

The two copies are what let this survive: a self-review pass fixed the
markdown it was looking at, while the review surface re-introduced the
same defect at render time from ``prd.json``. One implementation, with
the index actually used, keeps the two surfaces in agreement.
"""
from __future__ import annotations

from typing import Iterable

#: Bullets for the first ten entries; beyond that a plain ``(N)`` is used,
#: because ``⑪`` and friends are not reliably present in every font.
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"

#: The alternates separator used across the CPEA documents.
_SEPARATOR = "；"


def circled_list(items: Iterable[str]) -> str:
    """Join ``items`` into a ``① a；② b`` list.

    The marker comes from each item's position, so two alternatives are
    always distinguishable. Entries past the tenth fall back to ``(11)``
    rather than a wrapping or missing glyph.

    Returns an empty string for an empty input, so a caller can test the
    result directly instead of testing the input first.
    """
    rendered = []
    for index, item in enumerate(items):
        marker = (
            _CIRCLED[index] if index < len(_CIRCLED) else f"({index + 1})"
        )
        rendered.append(f"{marker} {item}")
    return _SEPARATOR.join(rendered)
