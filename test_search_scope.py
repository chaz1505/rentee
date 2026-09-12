"""Regression coverage with Bubble-shaped Geo -> Condo -> Listing records."""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")
import app
from search_flow import dump_search_state, empty_search_state


class SearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.geos = [{"_id": f"g{i}", "name": name} for i, name in enumerate(
            ["Mont Kiara", "Bangsar", "Damansara Heights", "Future District"])]
        self.condos = [{"_id": f"c{g}-{i}", "name": f"Development {g}-{i}", "Geo": f"g{g}"}
                       for g in range(4) for i in range(3)]
        self.condos[0]["name"] = "Arcoris"
        self.listings = [{"_id": f"{c['_id']}-{i}", "condo": c["_id"], "beds": 3,
                          "priceRent": 20000, "TransactionType": ["Rent/Let"],
                          "propertyType": "Condo"} for c in self.condos for i in range(2)]
        self.lead = {"searchActive": dump_search_state(self.state()),
                     "preferredCondos": ["historical-condo"], "Geo": ["g1"],
                     "TransactionType": ["Buy/Sell", "Rent/Let"], "budgetBuy": 7000000}
        self.calls = []
        self.patches = [patch("app.bubble", side_effect=self.bubble),
                        patch("app.get_valid_geo_names", return_value=[g["name"] for g in self.geos]),
                        patch("app.save_property_search_state", side_effect=self.save)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def state(self, area="Mont Kiara"):
        state = empty_search_state()
        state.update(areas=[area], area_status="known", property_types=["Condo", "rent"],
                     bedroom_requirement="3", budget_rent="25000", budget_requirement="25000")
        return state

    def save(self, lead_id, cumulative, base, lead_fields=None, active=None):
        self.lead.update(lead_fields or {})
        self.lead["searchBriefJSON"] = dump_search_state(cumulative)
        if active is not None:
            self.lead["searchActive"] = dump_search_state(active)

    def bubble(self, url, params=None):
        self.calls.append((url, params))
        if "/folio/" in url:
            return {"lead": "lead"}
        if "/lead/" in url:
            return dict(self.lead)
        kind = url.rsplit("/", 1)[1]
        rows = {"geo": self.geos, "condo": self.condos, "listing": self.listings}[kind]
        for c in json.loads((params or {}).get("constraints", "[]")):
            key, op, value = c["key"], c["constraint_type"], c.get("value")
            if op == "equals":
                rows = [r for r in rows if r.get(key) == value]
            elif op == "in":
                rows = [r for r in rows if r.get(key) in value]
            elif op == "contains":
                rows = [r for r in rows if value in r.get(key, [])]
            elif op == "greater than":
                rows = [r for r in rows if r.get(key, 0) > value]
            elif op == "is_not_empty":
                rows = [r for r in rows if r.get(key) is not None]
            else:
                self.fail(f"Unexpected operator {op}")
        cursor = (params or {}).get("cursor", 0)
        # Deliberately small pages exercise every relationship page.
        page = rows[cursor:cursor + 2]
        return {"results": page, "remaining": max(0, len(rows) - cursor - len(page))}

    def advance(self, message, **update):
        return app.advance_property_search("folio", "live", {
            "_user_message": message, "search_listings": True, **update})

    def retrieve(self, excluded=()):
        lead = app.lead_with_active_search_filters(self.lead, "https://bubble.test")
        lead["_excluded_listing_ids"] = set(excluded)
        return app.get_plausible_listings("https://bubble.test", lead)[0]

    def test_area_searches_include_all_three_condos(self):
        for area in [g["name"] for g in self.geos]:
            with self.subTest(area=area):
                result = self.advance(f"Try {area} instead", geo_names=[area])
                self.assertEqual(result["active_state"]["areas"], [area])
                self.assertEqual(result["active_state"]["selected_condos"], [])
                listings = self.retrieve()
                self.assertEqual(len({r["condo"] for r in listings}), 3)
                self.assertEqual(len(listings), 6)
                constraints = json.loads(self.calls[-1][1]["constraints"])
                scope = next(c for c in constraints if c["key"] == "condo")
                self.assertEqual(scope["constraint_type"], "in")
                self.assertEqual(len(scope["value"]), 3)
                self.assertNotIn("historical-condo", scope["value"])
                self.assertFalse(any(c["key"] == "Geo" for c in constraints))

    def test_buy_to_rent_and_bedroom_followup(self):
        state = self.state("Bangsar")
        state.update(property_types=["Condo", "buy"], budget_buy="7000000", budget_rent="",
                     budget_requirement="7000000")
        self.lead["searchActive"] = dump_search_state(state)
        self.advance("rental RM25k Mont Kiara", transaction_type="rent", budget_rent=25000,
                     geo_names=["Mont Kiara"])
        result = self.advance("3 beds", bedrooms_min=3, transaction_type="buy",
                              budget_buy=7000000, geo_names=["Bangsar"])
        active = result["active_state"]
        self.assertEqual(active["areas"], ["Mont Kiara"])
        self.assertEqual(active["property_types"], ["Condo", "rent"])
        self.assertEqual(active["budget_rent"], "25000")
        self.assertEqual(active["budget_buy"], "")
        self.assertEqual(active["bedroom_requirement"], "3")
        self.assertEqual(len(self.retrieve()), 6)

    def test_continuations_ignore_invented_constraints(self):
        for message in ("Yes see alternatives", "anything else?", "more options", "show me more",
                        "what else have you got?", "any others?", "other options?", "continue please"):
            with self.subTest(message=message):
                result = self.advance(message, preferred_condo_names=["Damansara Heights"],
                                      condo_update_mode="replace", area_update_mode="reset",
                                      transaction_type="buy", bedrooms_min=7, budget_buy=1,
                                      new_search=True, property_types=["Landed"])
                self.assertEqual(result["active_state"], self.state())
                self.assertIsNone(result["scope"])

    def test_alternatives_exclude_seen_and_continue_pagination(self):
        first = self.retrieve()[:3]
        state_before = self.lead["searchActive"]
        self.advance("show me alternatives", geo_names=["Future District"])
        additional = self.retrieve([r["_id"] for r in first])
        self.assertEqual(self.lead["searchActive"], state_before)
        self.assertEqual(len(additional), 3)
        self.assertFalse({r["_id"] for r in first} & {r["_id"] for r in additional})
        self.assertTrue(all(r["condo"].startswith("c0-") for r in additional))
        self.assertEqual(self.retrieve([r["_id"] for r in self.retrieve()]), [])

    def test_geo_labelled_condo_is_authoritatively_geo(self):
        result = self.advance("How about Damansara Heights?",
                              preferred_condo_names=["Damansara Heights"],
                              area_update_mode="reset", condo_update_mode="replace")
        self.assertEqual(result["active_state"]["areas"], ["Damansara Heights"])
        self.assertEqual(result["active_state"]["selected_condos"], [])
        self.assertEqual(len(self.retrieve()), 6)

    def test_explicit_condo_narrows_and_inherits(self):
        result = self.advance("Anything in Arcoris?", preferred_condo_names=["Arcoris"])
        active = result["active_state"]
        self.assertEqual(active["selected_condos"], ["Arcoris"])
        self.assertEqual(active["areas"], [])
        self.assertEqual(active["bedroom_requirement"], "3")
        self.assertEqual(active["budget_rent"], "25000")
        self.assertEqual({r["condo"] for r in self.retrieve()}, {"c0-0"})

    def test_scalar_refinements_preserve_other_constraints(self):
        result = self.advance("Make it 4 beds", bedrooms_min=4, budget_rent=100,
                              geo_names=["Bangsar"])
        self.assertEqual(result["active_state"]["bedroom_requirement"], "4")
        result = self.advance("Budget 15k", budget_rent=15000, bedrooms_min=8)
        self.assertEqual(result["active_state"]["bedroom_requirement"], "4")
        self.assertEqual(result["active_state"]["budget_rent"], "15000")
        self.assertEqual(result["active_state"]["areas"], ["Mont Kiara"])

    def test_unknown_explicit_entity_preserves_state_and_does_not_query_listings(self):
        result = self.advance("Anything in Unknown Tower?", preferred_condo_names=["Unknown Tower"])
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["active_state"], self.state())
        self.assertFalse(any(url.endswith("/listing") for url, _ in self.calls))

    def test_unresolved_condo_query_does_not_scan_listings(self):
        listings, fetched = app.get_plausible_listings("https://bubble.test", {}, ["Unknown Tower"])
        self.assertEqual((listings, fetched), ([], 0))
        self.assertFalse(any(url.endswith("/listing") for url, _ in self.calls))

    def test_legacy_geo_typed_as_condo_query_uses_relationships(self):
        listings, _ = app.get_plausible_listings("https://bubble.test", {}, ["Mont Kiara"])
        self.assertEqual(len({r["condo"] for r in listings}), 3)

    def test_empty_geo_never_becomes_unrestricted(self):
        self.condos = []
        self.assertEqual(self.retrieve(), [])
        self.assertFalse(any(url.endswith("/listing") for url, _ in self.calls))

    def test_unknown_saved_geo_does_not_widen(self):
        self.lead["searchActive"] = dump_search_state(self.state("Deleted Geo"))
        self.assertEqual(self.retrieve(), [])
        self.assertFalse(any(url.endswith("/listing") for url, _ in self.calls))

    def test_real_entity_collision_requires_clarification(self):
        self.condos.append({"_id": "collision", "name": "Mont Kiara", "Geo": "g0"})
        result = self.advance("Try Mont Kiara", geo_names=["Mont Kiara"])
        self.assertEqual(result["action"], "ask")

    def test_name_without_database_id_is_not_resolution(self):
        self.condos.append({"name": "Ghost Tower", "Geo": "g0"})
        result = self.advance("Try Ghost Tower", preferred_condo_names=["Ghost Tower"])
        self.assertEqual(result["action"], "ask")

    def test_large_scope_batches_without_dropping_ids(self):
        ids = [f"condo-{i}" for i in range(123)]
        plan = app.build_listing_bubble_constraints({}, ids)
        groups = [q[-1]["value"] for q in plan["queries"]]
        self.assertEqual([len(g) for g in groups], [50, 50, 23])
        self.assertEqual([i for g in groups for i in g], ids)

    def test_condo_proposed_as_geo_replaces_reset_mode_after_resolution(self):
        result = self.advance("Anything in Arcoris?", geo_names=["Arcoris"],
                              area_update_mode="replace", condo_update_mode="reset")
        self.assertEqual(result["active_state"]["selected_condos"], ["Arcoris"])
        self.assertEqual(result["active_state"]["areas"], [])

    def test_continuation_preserves_unresolved_saved_scope_without_broadening(self):
        self.lead["searchActive"] = dump_search_state(self.state("Deleted Geo"))
        self.advance("show more", geo_names=["Bangsar"])
        self.assertEqual(self.retrieve(), [])
        self.assertFalse(any(url.endswith("/listing") for url, _ in self.calls))

    def test_exclusions_are_applied_before_candidate_target(self):
        lead = app.lead_with_active_search_filters(self.lead, "https://bubble.test")
        lead["_excluded_listing_ids"] = {"c0-0-0", "c0-0-1"}
        listings, fetched = app.get_plausible_listings("https://bubble.test", lead, target=2)
        self.assertEqual(fetched, 4)
        self.assertEqual({r["_id"] for r in listings}, {"c0-1-0", "c0-1-1"})

    def test_explicit_typo_is_grounded_before_canonical_resolution(self):
        with patch("app.resolve_condo_mentions", return_value=[]):
            update = app._apply_current_search_location(
                "Try Mont Kaira instead", "live", {"geo_names": ["Mont Kiara"]})
        result = self.advance("Try Mont Kaira instead", **update)
        self.assertEqual(result["active_state"]["areas"], ["Mont Kiara"])
