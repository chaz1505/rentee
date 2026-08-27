"""Small durable memory helpers for Rentee property searches."""

import json


SEARCH_BRIEF_FIELDS = (
    "areas", "property_types", "bedroom_requirement", "budget_requirement",
    "other_requirements", "priorities",
)


def empty_search_state():
    return {
        "area_status": "unchanged",
        "areas": [],
        "regular_destinations": [],
        "area_recommendations": [],
        "property_types": [],
        "bedroom_requirement": "",
        "budget_requirement": "",
        "other_requirements": [],
        "other_requirements_answered": False,
        "priorities": [],
        "priorities_answered": False,
        "recommended_condos": [],
        "selected_condos": [],
        "liked_condos": [],
        "disliked_condos": [],
        "preference_notes": [],
        # Small channel metadata persisted with the existing durable search brief.
        # It is not customer search criteria and is ignored by recommendation logic.
        "channel_state": {},
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
                state[key] = source[key]
        # Migrate briefs saved before area_status became explicit.
        if "area_status" not in source and source.get("area_unknown") is True:
            state["area_status"] = "unknown"
        elif state["areas"]:
            state["area_status"] = "known"
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
                material_change |= value != state[key]
            elif value != state[key]:
                state["area_recommendations"] = []
            state[key] = value
    for key in ("bedroom_requirement", "budget_requirement"):
        if str(update.get(key) or "").strip():
            value = str(update[key]).strip()
            material_change |= value != state[key]
            state[key] = value

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
