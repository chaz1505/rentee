"""Small durable memory helpers for Rentee property searches."""

import json
import copy


SEARCH_BRIEF_FIELDS = (
    "areas", "transaction_type", "property_types", "bedroom_requirement", "budget_requirement",
    "budget_rent", "budget_buy",
    "other_requirements", "priorities",
)


def empty_search_state():
    return {
        "geography_provenance": {},
        "pending_broadening": {},
        "scope_needs_clarification": False,
        "area_status": "unchanged",
        "areas": [],
        "regular_destinations": [],
        "area_recommendations": [],
        "property_types": [],
        "transaction_type": "",
        "bedroom_requirement": "",
        "budget_requirement": "",
        "budget_rent": "",
        "budget_buy": "",
        "other_requirements": [],
        "other_requirements_answered": False,
        "priorities": [],
        "priorities_answered": False,
        "recommended_condos": [],
        "selected_condos": [],
        "liked_condos": [],
        "disliked_condos": [],
        "preference_notes": [],
    }


def load_search_state(value):
    if isinstance(value, dict):
        source = value
    else:
        try:
            source = json.loads(value or "{}")
        except (TypeError, ValueError):
            source = {}
    state = empty_search_state()
    if isinstance(source, dict):
        for key in state:
            if key in source and isinstance(source[key], type(state[key])):
                state[key] = copy.deepcopy(source[key])
        # Older states stored transaction tokens alongside home types. Migrate
        # them into their own canonical field and never persist them back there.
        raw_types = source.get("property_types", [])
        rendered_types = " ".join(str(item).casefold() for item in raw_types)
        migrated_modes = set()
        if "rent" in rendered_types or "let" in rendered_types:
            migrated_modes.add("rent")
        if any(word in rendered_types for word in ("buy", "sale", "purchas")):
            migrated_modes.add("buy")
        if not state["transaction_type"] and migrated_modes:
            state["transaction_type"] = (
                "both" if migrated_modes == {"rent", "buy"} else next(iter(migrated_modes))
            )
        state["property_types"] = [
            item for item in state["property_types"]
            if str(item).casefold() not in {
                "rent", "let", "rent/let", "buy", "sale", "buy/sell", "purchase", "both"
            }
        ]
        # Migrate briefs saved before area_status became explicit.
        if "area_status" not in source and source.get("area_unknown") is True:
            state["area_status"] = "unknown"
        elif state["areas"]:
            state["area_status"] = "known"
        # Migrate the old generic budget only when its transaction is unambiguous.
        # This preserves existing searches without ever turning a rental budget into
        # a purchase budget (or vice versa) after the active mode changes.
        if (source.get("budget_requirement")
                and "budget_rent" not in source and "budget_buy" not in source):
            transaction = state["transaction_type"]
            is_rent = transaction in {"rent", "both"}
            is_buy = transaction in {"buy", "both"}
            if is_rent and not is_buy:
                state["budget_rent"] = str(source["budget_requirement"])
            elif is_buy and not is_rent:
                state["budget_buy"] = str(source["budget_requirement"])
    if state["area_status"] not in ("unchanged", "known", "unknown"):
        state["area_status"] = "known" if state["areas"] else "unchanged"
    return state


def dump_search_state(state):
    return json.dumps(load_search_state(state), ensure_ascii=False, separators=(",", ":"))


def apply_search_update(state, update):
    state = load_search_state(state)
    update = update or {}
    material_change = False

    area_status = update.get("area_status", "unchanged")
    if area_status == "known" and update.get("areas"):
        areas = _unique(update["areas"])
        material_change |= areas != state["areas"]
        state["areas"] = areas
        state["area_status"] = "known"
        state["area_recommendations"] = []
    elif area_status == "unknown":
        material_change |= bool(state["areas"])
        state["areas"] = []
        state["area_status"] = "unknown"

    for key in ("property_types", "regular_destinations"):
        if update.get(key):
            value = _unique(update[key])
            if key == "property_types":
                rendered = " ".join(item.casefold() for item in value)
                modes = set()
                if "rent" in rendered or "let" in rendered:
                    modes.add("rent")
                if any(word in rendered for word in ("buy", "sale", "purchas")):
                    modes.add("buy")
                if modes and not update.get("transaction_type"):
                    state["transaction_type"] = (
                        "both" if modes == {"rent", "buy"} else next(iter(modes))
                    )
                value = [item for item in value if item.casefold() not in {
                    "rent", "let", "rent/let", "buy", "sale", "buy/sell", "purchase", "both"
                }]
                material_change |= value != state[key]
            elif value != state[key]:
                state["area_recommendations"] = []
            state[key] = value
    for key in (
        "transaction_type", "bedroom_requirement", "budget_requirement", "budget_rent", "budget_buy",
    ):
        if str(update.get(key) or "").strip():
            value = str(update[key]).strip()
            material_change |= value != state[key]
            state[key] = value
    if update.get("budget_requirement") and not (
        update.get("budget_rent") is not None or update.get("budget_buy") is not None
    ):
        if state["transaction_type"] == "rent":
            state["budget_rent"] = str(update["budget_requirement"]).strip()
        elif state["transaction_type"] == "buy":
            state["budget_buy"] = str(update["budget_requirement"]).strip()

    if update.get("other_requirements_answered"):
        state["other_requirements_answered"] = True
        new_requirements = _unique(update.get("other_requirements", []))
        combined_requirements = _unique(state["other_requirements"] + new_requirements)
        material_change |= combined_requirements != state["other_requirements"]
        state["other_requirements"] = combined_requirements
    if update.get("priorities_answered"):
        state["priorities_answered"] = True
        state["priorities"] = _unique(update.get("priorities", []))[:3]

    for key in ("liked_condos", "disliked_condos", "preference_notes"):
        additions = _unique(update.get(key, []))
        if additions:
            combined = _unique(state[key] + additions)
            state[key] = combined

    if material_change:
        state["recommended_condos"] = []
        state["selected_condos"] = []

    selected = _unique(update.get("selected_condos", []))
    if selected:
        allowed = {name.casefold() for name in state["recommended_condos"]}
        state["selected_condos"] = [name for name in selected if name.casefold() in allowed]

    return state


def set_area_recommendations(state, recommendations):
    state = load_search_state(state)
    cleaned = []
    seen = set()
    for item in recommendations or []:
        if not isinstance(item, dict):
            continue
        area_name = " ".join(str(item.get("area_name") or "").split())
        reason = " ".join(str(item.get("reason") or "").split())
        key = area_name.casefold()
        if area_name and reason and key not in seen:
            cleaned.append({"area_name": area_name, "reason": reason})
            seen.add(key)
    state["area_recommendations"] = cleaned[:4]
    return state


def set_recommended_condos(state, condo_names):
    state = load_search_state(state)
    state["recommended_condos"] = _unique(condo_names)
    state["selected_condos"] = []
    return state


def listing_search_scope(state, selected_condos=None, use_full_shortlist=False):
    state = load_search_state(state)
    shortlist = state["recommended_condos"]
    if not shortlist:
        return []
    disliked = {name.casefold() for name in state["disliked_condos"]}
    shortlist = [name for name in shortlist if name.casefold() not in disliked]
    selected = _unique(selected_condos or state["selected_condos"])
    if selected and not use_full_shortlist:
        allowed = {name.casefold() for name in shortlist}
        return [name for name in selected if name.casefold() in allowed]
    if use_full_shortlist:
        return list(shortlist)
    return []


def _unique(values):
    result, seen = [], set()
    for value in values or []:
        clean = " ".join(str(value or "").split())
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result
