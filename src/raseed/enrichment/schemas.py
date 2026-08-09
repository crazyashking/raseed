"""The Stage 2 categorization contract.

Separate schema, separate call, separate prompt. Invariant 4. Stage 2 never sees
the image and never sees a price: it is handed a list of printed product names
and returns a category for each. That separation is what makes it free to re-run
the whole history when the taxonomy changes, since nothing here needs the image
back.

Two properties are deliberate:

**Field order is part of the contract, same as Stage 1.** `index` comes first so
the model is committed to which item it is talking about before it commits to a
category, and `category` precedes `confidence` so the confidence is a report on
a decision already made rather than a number the category is then fitted to.

**There is no free text.** No reasoning field, no label, no canonical name. The
model returns an index, a slug from a closed set, and a number. Nothing it emits
can become a stored string, which keeps brief 16.10's prompt-injection surface at
zero for this stage: a receipt item named "ignore previous instructions" can at
worst be assigned the wrong category.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class _Frozen(BaseModel):
    """Base config for every Stage 2 model. Same reasoning as Stage 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemCategory(_Frozen):
    """One item's category, as the model decided it."""

    index: Annotated[StrictInt, Field(ge=0)] = Field(
        description="The zero-based position of the item in the list you were given."
    )
    category: str = Field(
        description="Exactly one of the allowed category slugs, copied character for character."
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "How sure you are, from 0 to 1. Be honest and use low numbers freely: "
            "anything below the caller's threshold is filed as uncategorized rather "
            "than guessed, and that is the correct outcome for an item you do not "
            "recognise."
        )
    )


class CategorizationResult(_Frozen):
    """What Stage 2 returns for one batch of product names."""

    items: list[ItemCategory] = Field(
        description="One entry per item you were given, in the same order.",
    )


__all__ = ["CategorizationResult", "ItemCategory"]
