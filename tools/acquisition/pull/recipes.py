"""tools/acquisition/pull/recipes.py — named PropStream filter recipes.

A recipe is a declarative list of (action, logical_key, value) steps executed
against the PageDriver seam — selectors stay data in tools/browser (overlay
``browser.yaml``), so a recipe never contains a CSS selector. Every recipe
includes ownership-length >= 5 years (PropStream supports it as a filter);
where a wanted criterion has no PropStream filter (e.g. land >80% of total
value for teardowns), the recipe documents it in ``manual_notes`` and the
post-pull screener enforces it instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OWNERSHIP_YEARS_MIN = 5


@dataclass(frozen=True)
class Recipe:
    name: str
    description: str
    # (action, key, value): action in {"click", "fill"}; value only for fill.
    steps: tuple = ()
    # Criteria PropStream can't filter server-side; enforced downstream and
    # listed on the manual checklist.
    manual_notes: tuple = ()
    # The default vacant-land recipe uses the existing tested flow instead of
    # generic steps (it predates recipes and is calibrated first).
    uses_builtin_filters: bool = True


_OWNERSHIP_STEP = ("fill", "filters.ownership_years_min", str(OWNERSHIP_YEARS_MIN))


RECIPES: dict[str, Recipe] = {
    "vacant_land": Recipe(
        name="vacant_land",
        description="The current production recipe: Property Class = Vacant "
                    "Land + the BrowserConfig filter stack (lot size, "
                    "assessed value, absentee, ...), ownership >= 5y.",
        steps=(_OWNERSHIP_STEP,),
        uses_builtin_filters=True,
    ),
    "teardown_ratio": Recipe(
        name="teardown_ratio",
        description="Improved parcels priced as dirt: improvement value "
                    "< $20K (the structure is worthless — the lot is the "
                    "value), ownership >= 5y.",
        steps=(
            ("click", "filters.property_class"),
            ("click", "filters.property_class_option_improved"),
            ("fill", "filters.improvement_max", "20000"),
            _OWNERSHIP_STEP,
        ),
        manual_notes=(
            "ALTERNATIVE teardown signal PropStream can't filter: land value "
            ">= 80% of total assessed — screener enforces post-pull.",
        ),
        uses_builtin_filters=False,
    ),
    "commercial_vacant": Recipe(
        name="commercial_vacant",
        description="Vacant commercial-zoned land, ownership >= 5y.",
        steps=(
            ("click", "filters.property_class"),
            ("click", "filters.property_class_option_commercial"),
            _OWNERSHIP_STEP,
        ),
        uses_builtin_filters=False,
    ),
}


def get_recipe(name: str) -> Recipe:
    try:
        return RECIPES[name]
    except KeyError:
        raise ValueError(
            f"unknown recipe {name!r} — available: {sorted(RECIPES)}") from None


def manual_checklist(county: str, state: str, recipe: Recipe) -> str:
    """A human-runnable pull checklist — the fallback when automation stops."""

    lines = [
        f"# Manual PropStream pull — {county.title()} County, {state.upper()}",
        f"Recipe: **{recipe.name}** — {recipe.description}", "",
        f"1. Search: `{county.title()} County, {state.upper()}`",
    ]
    n = 2
    if recipe.uses_builtin_filters:
        lines.append(f"{n}. Apply the standard vacant-land filter stack "
                     "(Property Class = Vacant Land + your saved defaults)")
        n += 1
    for action, key, *value in recipe.steps:
        pretty = key.split(".")[-1].replace("_", " ")
        if action == "fill":
            lines.append(f"{n}. Set {pretty} = {value[0]}")
        else:
            lines.append(f"{n}. Select {pretty}")
        n += 1
    lines += [f"{n}. Select all -> Add to list -> skip trace -> Export CSV",
              f"{n + 1}. Drop the CSV into /root/leads_inbox (intake takes over)"]
    if recipe.manual_notes:
        lines += ["", "Notes:"] + [f"- {note}" for note in recipe.manual_notes]
    return "\n".join(lines)
