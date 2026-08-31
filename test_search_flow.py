import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module
from search_flow import (
    apply_search_update,
    dump_search_state,
    empty_search_state,
    load_search_state,
    listing_search_scope,
    set_recommended_condos,
)


def complete_state():
    return apply_search_update(empty_search_state(), {
        "area_status": "known",
        "areas": ["Bangsar"],
        "property_types": ["Condo"],
        "bedroom_requirement": "exactly 4 bedrooms",
        "budget_requirement": "maximum RM12,000",
        "other_requirements": ["furnished", "balcony"],
        "other_requirements_answered": True,
        "priorities": ["location", "bedrooms", "balcony"],
        "priorities_answered": True,
    })


class SearchFlowStateTests(unittest.TestCase):
    def setUp(self):
        app_module._geo_name_cache.clear()
        self.valid_geos_patcher = patch(
            "app.get_valid_geo_names",
            return_value=["Bangsar", "KLCC", "Mont Kiara"],
        )
        self.valid_geos_patcher.start()
        self.addCleanup(self.valid_geos_patcher.stop)

    def test_geo_resolution_exact_case_whitespace_and_safe_typo(self):
        valid = ["Bangsar", "KLCC", "Mont Kiara"]
        result = app_module.resolve_geo_names(
            ["Bangsar", "bangsar", "  Mont   Kiara ", "Mont Kaira"], valid
        )
        self.assertEqual(result["resolved"], ["Bangsar", "Mont Kiara"])
        self.assertEqual(result["unresolved"], [])

    def test_bexley_and_unknown_never_resolve_when_not_canonical(self):
        result = app_module.resolve_geo_names(
            ["Bexley", "Imaginary Heights"], ["Bangsar", "KLCC", "Mont Kiara"]
        )
        self.assertEqual(result["resolved"], [])
        self.assertEqual(result["unresolved"], ["Bexley", "Imaginary Heights"])

    def test_mixed_geo_candidates_only_return_canonical_values(self):
        result = app_module.resolve_geo_names(
            ["klcc", "Bexley", "bangsar"], ["Bangsar", "KLCC"]
        )
        self.assertEqual(result["resolved"], ["KLCC", "Bangsar"])
        self.assertEqual(result["unresolved"], ["Bexley"])

    def test_geo_vocabulary_cache_uses_bubble_geo_names(self):
        self.valid_geos_patcher.stop()
        pages = iter([{
            "results": [
                {"_id": "geo-1", "name": "Bangsar"},
                {"_id": "geo-2", "Name": "KLCC"},
            ],
            "remaining": 0,
        }])
        with patch("app.bubble", side_effect=lambda *_args, **_kwargs: next(pages)) as get:
            first = app_module.get_valid_geo_names("live", now=100)
            second = app_module.get_valid_geo_names("live", now=101)
        self.assertEqual(first, ["Bangsar", "KLCC"])
        self.assertEqual(second, first)
        self.assertEqual(get.call_count, 1)

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids")
    @patch("app.bubble")
    def test_bexley_is_not_saved_or_passed_to_listing_search(
        self, bubble, named_ids, save,
    ):
        bubble.side_effect = [
            {"lead": "lead-1"},
            {"_id": "lead-1", "searchBriefJSON": "", "searchActive": ""},
        ]
        result = app_module.advance_property_search("folio-1", "live", {
            "geo_names": ["Bexley"], "area_update_mode": "replace",
            "search_listings": True,
        })
        self.assertEqual(result["action"], "ask")
        self.assertNotIn("Bexley", result["state"]["areas"])
        self.assertNotIn("Bexley", result["active_state"]["areas"])
        self.assertEqual(result["geo_resolution"]["unresolved"], ["Bexley"])
        named_ids.assert_not_called()
        saved_payload = save.call_args.args
        self.assertNotIn("Geo", saved_payload[3])

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["geo-bangsar"])
    @patch("app.bubble")
    def test_valid_bangsar_continues_to_listing_search(
        self, bubble, _named_ids, _save,
    ):
        bubble.side_effect = [
            {"lead": "lead-1"},
            {"_id": "lead-1", "searchBriefJSON": "", "searchActive": ""},
        ]
        result = app_module.advance_property_search("folio-1", "live", {
            "geo_names": ["bangsar"], "area_update_mode": "replace",
            "search_listings": True,
        })
        self.assertEqual(result["action"], "search_listings")
        self.assertEqual(result["active_state"]["areas"], ["Bangsar"])

    @patch("app.save_search_state")
    @patch("app.bubble")
    def test_existing_valid_persisted_area_remains_canonical(
        self, bubble, _save,
    ):
        stored = dump_search_state(apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
        }))
        bubble.side_effect = [
            {"lead": "lead-1"},
            {"_id": "lead-1", "searchBriefJSON": stored,
             "searchActive": stored},
        ]
        result = app_module.advance_property_search(
            "folio-1", "live", {"question": "What budget?"}
        )
        self.assertEqual(result["state"]["areas"], ["Bangsar"])
        self.assertEqual(result["active_state"]["areas"], ["Bangsar"])

    @patch("app.get_relationship_names", return_value={"condo-1": "One Menerung"})
    @patch("app.bubble")
    def test_current_recommendations_reuse_folio_furnishing_and_size_read_only(
        self, mocked_bubble, _mocked_names
    ):
        records = {
            "/obj/folio/folio-1": {"folioItems": ["item-1", "item-2"]},
            "/obj/folioItem/item-1": {"listing": "listing-1"},
            "/obj/folioItem/item-2": {"listing": "listing-2"},
            "/obj/listing/listing-1": {
                "_id": "listing-1", "condo": "condo-1", "beds": 3,
                "priceRent": 9000, "Furnishing": "Fully furnished", "Sq Ft": 1500,
            },
            "/obj/listing/listing-2": {
                "_id": "listing-2", "condoName": "Ken Bangsar", "beds": 3,
                "priceRent": 8500, "furnished": "Partly furnished", "size": 1800,
            },
        }
        mocked_bubble.side_effect = lambda url, **_kwargs: next(
            value for suffix, value in records.items() if url.endswith(suffix)
        )

        result = json.loads(app_module.get_current_recommendations("folio-1", "live"))

        listings = result["current_recommendations"]
        self.assertEqual(listings[0]["condo_name"], "One Menerung")
        self.assertEqual(listings[0]["furnishing"], "Fully furnished")
        self.assertEqual(listings[1]["furnishing"], "Partly furnished")
        self.assertEqual(listings[1]["size"], 1800)
        self.assertEqual([item["position"] for item in listings], [1, 2])
        self.assertEqual(mocked_bubble.call_count, 5)

    @patch("app.execute_match_lead_silently")
    @patch("app.get_current_recommendations")
    def test_furnishing_followup_uses_current_recommendations_not_matching(
        self, mocked_current, mocked_match
    ):
        mocked_current.return_value = json.dumps({"current_recommendations": [{
            "property_name": "One Menerung", "furnishing": "Fully furnished",
        }]})
        tool_call = SimpleNamespace(
            type="function_call", name="get_current_recommendations",
            call_id="current-call", arguments="{}",
        )
        initial = SimpleNamespace(id="initial", output=[tool_call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial),
            FakeStream(final, [SimpleNamespace(
                type="response.output_text.delta",
                delta="One Menerung is fully furnished.",
            )]),
        ]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post("/chat_stream", json={
                "message": "which of these are furnished?", "folio_id": "folio-1",
            })

        self.assertIn("One Menerung is fully furnished", response.get_data(as_text=True))
        mocked_current.assert_called_once_with("folio-1", "live")
        mocked_match.assert_not_called()
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertIn("current_recommendations", continuation["input"][0]["output"])

    def test_tool_contract_separates_shortlist_questions_from_new_search(self):
        tools = {tool.get("name"): tool for tool in
                 app_module.build_response_args("test")["tools"]}
        current = tools["get_current_recommendations"]["description"]
        search = tools["advance_property_search"]["description"]
        self.assertIn("already recommended", current)
        self.assertIn("read-only", current)
        self.assertIn("changes the search", search)

    @patch("app.advance_property_search")
    def test_chat_guided_search_uses_structured_search_action(self, mocked_advance):
        tool_args = {
            "area_status": "unchanged", "areas": [],
            "regular_destinations": [], "property_types": [],
            "bedroom_requirement": "4 bedrooms", "budget_requirement": "",
            "other_requirements": [], "other_requirements_answered": False,
            "priorities": [], "priorities_answered": False,
            "selected_condos": [], "use_full_shortlist": False,
            "search_listings": False,
        }
        tool_call = SimpleNamespace(
            type="function_call", name="advance_property_search",
            call_id="search-call", arguments=json.dumps(tool_args),
        )
        initial = SimpleNamespace(id="initial", output=[tool_call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)
        mocked_advance.return_value = {
            "action": "ask", "text": "What's your monthly rental budget?",
            "state": {}, "lead_id": "lead-1",
        }

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial),
            FakeStream(final, [SimpleNamespace(
                type="response.output_text.delta", delta="What's your budget?"
            )]),
        ]

        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": "I need 4 bedrooms", "folio_id": "folio-1"},
            )
            body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("What's your budget?", body)
        mocked_advance.assert_called_once_with("folio-1", "live", tool_args)
        responses.create.assert_not_called()

    @patch("app.save_search_state")
    @patch(
        "app.execute_match_lead_silently",
        return_value="I’m sorry, I couldn’t prepare your recommendations just now.",
    )
    @patch("app.advance_property_search")
    def test_chat_listing_action_executes_matching_in_same_turn(
        self, mocked_advance, mocked_match, _mocked_save
    ):
        tool_args = {"search_listings": True}
        tool_call = SimpleNamespace(
            type="function_call", name="advance_property_search",
            call_id="search-call", arguments=json.dumps(tool_args),
        )
        initial = SimpleNamespace(id="initial", output=[tool_call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)
        mocked_advance.return_value = {
            "action": "search_listings", "scope": ["One Menerung"],
            "state": {}, "lead_id": "lead-1",
        }

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial),
            FakeStream(final, [SimpleNamespace(
                type="response.output_text.delta", delta="Here are the current matches."
            )]),
        ]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post("/chat_stream", json={
                "message": "show me the matches", "folio_id": "folio-1",
                "message_id": "message-1",
            })
            body = response.get_data(as_text=True)

        self.assertIn("Here are the current matches.", body)
        mocked_match.assert_called_once_with(
            "folio-1", "live", "message-1", ["One Menerung"]
        )
        continuation = responses.stream.call_args_list[1].kwargs
        tool_output = continuation["input"][0]["output"]
        self.assertIn("couldn’t prepare", tool_output)
        self.assertNotIn("curated selection", tool_output)
        self.assertIn('"recommendations_relevant": false', body)

    @patch("app.save_search_state")
    @patch("app.recommend_areas_for_search")
    @patch("app.bubble")
    def test_advance_search_recommends_areas_from_stored_destinations(
        self, mocked_bubble, mocked_recommend, mocked_save
    ):
        stored = apply_search_update(empty_search_state(), {
            "area_status": "unchanged",
            "regular_destinations": ["KLCC", "Alice Smith School"],
            "property_types": ["Condo"],
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]
        mocked_recommend.return_value = [
            {"area_name": "Bangsar", "reason": "Practical for both destinations."},
            {"area_name": "Damansara Heights", "reason": "A quieter alternative."},
        ]

        result = app_module.advance_property_search("folio-1", "live", {
            "recommend_areas": True,
        })

        self.assertEqual(result["action"], "recommend_areas")
        self.assertEqual(
            result["state"]["regular_destinations"],
            ["KLCC", "Alice Smith School"],
        )
        self.assertIn("Bangsar", result["text"])
        self.assertNotIn("Do you already know", result["text"])
        self.assertNotIn("Where do you", result["text"])
        mocked_save.assert_called_once()

    @patch("app.save_search_state")
    @patch("app.recommend_areas_for_search")
    @patch("app.bubble")
    def test_later_requirement_reuses_saved_area_recommendations(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        stored = apply_search_update(empty_search_state(), {
            "area_status": "unknown", "regular_destinations": ["KLCC"],
        })
        stored["area_recommendations"] = [
            {"area_name": "Bangsar", "reason": "A balanced option."}
        ]
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]

        result = app_module.advance_property_search("folio-1", "live", {
            "recommend_areas": True, "property_types": ["Condo"]
        })

        self.assertEqual(result["action"], "recommend_areas")
        self.assertEqual(result["state"]["area_status"], "unknown")
        self.assertEqual(result["state"]["property_types"], ["Condo"])
        mocked_recommend.assert_not_called()

    def test_material_change_invalidates_condo_shortlist(self):
        state = set_recommended_condos(complete_state(), ["One Menerung", "Ken Bangsar"])
        changed = apply_search_update(state, {"budget_requirement": "maximum RM9,000"})
        self.assertEqual(changed["recommended_condos"], [])

    def test_preference_added_later_preserves_existing_requirements(self):
        state = apply_search_update(empty_search_state(), {
            "other_requirements": ["furnished", "balcony"],
            "other_requirements_answered": True,
        })
        updated = apply_search_update(state, {
            "other_requirements": ["dog allowed"],
            "other_requirements_answered": True,
        })
        self.assertEqual(
            updated["other_requirements"],
            ["furnished", "balcony", "dog allowed"],
        )

    def test_customer_reaction_is_retained_and_filters_listing_scope(self):
        state = set_recommended_condos(
            complete_state(), ["One Menerung", "Ken Bangsar", "The Loft"]
        )
        updated = apply_search_update(state, {
            "liked_condos": ["One Menerung"],
            "disliked_condos": ["The Loft"],
            "preference_notes": ["Does not like dense developments", "Values space"],
        })
        self.assertEqual(updated["liked_condos"], ["One Menerung"])
        self.assertEqual(updated["disliked_condos"], ["The Loft"])
        self.assertIn("Values space", updated["preference_notes"])
        self.assertEqual(
            listing_search_scope(updated, use_full_shortlist=True),
            ["One Menerung", "Ken Bangsar"],
        )

    @patch("app.save_search_state")
    @patch("app.bubble")
    def test_model_can_choose_a_useful_question(self, mocked_bubble, _mocked_save):
        mocked_bubble.side_effect = [{"lead": "lead-1"}, {"searchBriefJSON": ""}]
        result = app_module.advance_property_search("folio-1", "live", {
            "bedroom_requirement": "3 bedrooms",
            "question": "Where do you need to travel most days?",
        })
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["text"], "Where do you need to travel most days?")

    @patch("app.save_search_state")
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_model_can_recommend_before_every_field_is_known(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        mocked_bubble.side_effect = [{"lead": "lead-1"}, {"searchBriefJSON": ""}]
        mocked_recommend.return_value = (
            [{"condo_name": "One Menerung", "reason": "Strong family fit"}],
            "One Menerung is a strong place to start.",
        )
        result = app_module.advance_property_search("folio-1", "live", {
            "regular_destinations": ["Bangsar South", "Garden International School"],
            "bedroom_requirement": "3 bedrooms",
            "budget_requirement": "around RM12k",
            "recommend_condos": True,
        })
        self.assertEqual(result["action"], "condo_shortlist")
        mocked_recommend.assert_called_once()

    @patch("app.save_search_state")
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_just_recommend_uses_saved_context_and_customer_reaction(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        stored = set_recommended_condos(
            complete_state(), ["One Menerung", "The Loft"]
        )
        stored = apply_search_update(stored, {
            "disliked_condos": ["The Loft"],
            "preference_notes": ["Prefers lower-density developments"],
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]
        mocked_recommend.return_value = (
            [{"condo_name": "Ken Bangsar", "reason": "Better fit"}],
            "Ken Bangsar is the better next option.",
        )
        result = app_module.advance_property_search("folio-1", "live", {
            "recommend_condos": True,
        })
        recommended_state = mocked_recommend.call_args.args[0]
        self.assertEqual(result["action"], "condo_shortlist")
        self.assertEqual(recommended_state["disliked_condos"], ["The Loft"])
        self.assertIn(
            "Prefers lower-density developments",
            recommended_state["preference_notes"],
        )

    def test_active_instructions_are_small_and_skill_based(self):
        instructions = app_module.build_response_args("Help me find a home")["instructions"]
        self.assertLess(len(instructions), 9000)
        self.assertIn("# Forwarded Listing Enquiry", instructions)
        self.assertIn("# Property Search", instructions)
        self.assertIn("# Condo advice", instructions)
        self.assertNotIn("NEW_PROPERTY_SEARCH", instructions)
        self.assertNotIn("function_call_output", instructions)

    def test_structured_listing_shortlist_uses_real_fields_and_budget_tier(self):
        lead = {
            "TransactionType": ["Rent/Let"],
            "bedroomsMin": 4,
            "budgetRent": 15000,
            "Geo": ["geo-bangsar"],
            "preferredCondos": [],
            "AIsearchtext": "This generated prose must not drive matching",
        }
        listings = [
            {"_id": "strong", "beds": 4, "priceRent": 14500, "Geo": "geo-bangsar"},
            {"_id": "too-small", "beds": 3, "priceRent": 14000, "Geo": "geo-bangsar"},
            {"_id": "wrong-tier", "beds": 4, "priceRent": 5000, "Geo": "geo-bangsar"},
            {"_id": "too-far-over", "beds": 4, "priceRent": 19000, "Geo": "geo-bangsar"},
        ]
        shortlisted = app_module.shortlist_structured_listings(lead, listings)
        self.assertEqual([item["_id"] for item in shortlisted], ["strong"])

    def test_candidate_reduction_caps_ranking_input_and_keeps_budget_fit(self):
        lead = {
            "TransactionType": ["Rent/Let"], "bedroomsMin": 3,
            "budgetRent": 15000,
        }
        listings = [
            {
                "_id": f"listing-{index}", "beds": 3,
                "priceRent": 7000 + index * 50,
                "condo": f"condo-{index % 8}",
            }
            for index in range(200)
        ]
        candidates = app_module.reduce_listing_candidates(lead, listings)
        self.assertEqual(len(candidates), app_module.RANKING_CANDIDATE_LIMIT)
        candidate_prices = [item["priceRent"] for item in candidates]
        self.assertIn(15000, candidate_prices)
        self.assertLess(max(abs(price - 15000) for price in candidate_prices), 1200)

    def test_listing_facts_keep_internal_id_and_add_resolved_condo_name(self):
        facts = app_module.listing_facts({
            "_id": "1767955404889x211582",
            "condo": "condo-id", "beds": 4, "priceRent": 11500,
            "Sq Ft": 3200, "Furnishing": "Partially furnished",
        }, {"condo-id": "One Menerung"})
        self.assertEqual(facts["_id"], "1767955404889x211582")
        self.assertEqual(facts["condo_name"], "One Menerung")
        self.assertEqual(facts["property_name"], "One Menerung")
        self.assertEqual(facts["beds"], 4)
        self.assertEqual(facts["priceRent"], 11500)

    def test_ranking_facts_use_only_canonical_listing_id(self):
        facts = app_module.ranking_listing_facts({
            "_id": "1767955404889x211582", "id": "wrong-id",
            "listing_id": "also-wrong", "beds": 3,
        })
        self.assertEqual(facts["listing_id"], "1767955404889x211582")
        self.assertNotIn("_id", facts)
        self.assertNotIn("id", facts)

    @patch("app.update_folio_items")
    @patch("app.create_folio_items", return_value=["folio-item-new"])
    @patch("app.get_plausible_listings")
    @patch("app.bubble")
    def test_recommendation_uses_name_and_reason_without_exposing_listing_id(
        self, mocked_bubble, mocked_listings, _mocked_create, _mocked_update
    ):
        listing_id = "1767955404889x211582"
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": []},
            {"TransactionType": ["Rent/Let"], "bedroomsMin": 4, "budgetRent": 12000},
            {"results": [{"_id": "condo-id", "name": "One Menerung"}], "remaining": 0},
        ]
        mocked_listings.return_value = ([{
            "_id": listing_id, "condo": "condo-id", "beds": 4,
            "priceRent": 11500, "Sq Ft": 3200,
        }], 1)
        model_response = SimpleNamespace(
            output_text=json.dumps({
                "recommendations": [{
                    "listing_id": listing_id,
                    "reco_summary": "It meets the four-bedroom need and budget.",
                }],
                "customer_response": (
                    f"{listing_id} is a strong fit because it has four bedrooms "
                    "and rents for RM11,500."
                ),
            }),
            usage=None,
        )
        with patch.object(app_module.client.responses, "create", return_value=model_response):
            flow = app_module.match_lead("folio-1", "live", "message-1")
            while True:
                try:
                    next(flow)
                except StopIteration as completed:
                    answer = completed.value
                    break
        self.assertNotIn(listing_id, answer)
        self.assertIn("One Menerung", answer)
        self.assertIn("four bedrooms", answer)
        self.assertIn("RM11,500", answer)
        self.assertTrue(answer.recommendations_available)

    @patch("app.bubble", return_value={"folioItems": []})
    def test_empty_current_recommendations_always_return_json(self, _mocked_bubble):
        result = json.loads(app_module.get_current_recommendations("folio-1", "live"))
        self.assertEqual(result, {"current_recommendations": []})

    @patch("app.bubble")
    def test_listing_retrieval_stops_after_plausible_pool_is_large_enough(
        self, mocked_bubble
    ):
        page_one = [{"_id": f"a-{index}"} for index in range(60)]
        page_two = [{"_id": f"b-{index}"} for index in range(60)]
        mocked_bubble.side_effect = [
            {"results": page_one, "remaining": 120},
            {"results": page_two, "remaining": 60},
        ]
        listings, fetched = app_module.get_plausible_listings(
            "https://bubble.test", {}, target=100
        )
        self.assertEqual(fetched, 120)
        self.assertEqual(len(listings), 120)
        self.assertEqual(mocked_bubble.call_count, 2)

    def test_preferred_condo_relationship_constrains_structured_shortlist(self):
        lead = {"preferredCondos": ["condo-one", "condo-loft"]}
        listings = [
            {"_id": "one", "condo": "condo-one"},
            {"_id": "loft", "condo": "condo-loft"},
            {"_id": "other", "condo": "condo-other"},
        ]
        shortlisted = app_module.shortlist_structured_listings(lead, listings)
        self.assertEqual([item["_id"] for item in shortlisted], ["one", "loft"])

    @patch("app.bubble", side_effect=app_module.requests.HTTPError("404 geo unavailable"))
    def test_unavailable_geo_api_does_not_abort_structured_update(self, _mocked_bubble):
        update = app_module.structured_lead_update({
            "transaction_type": "rent",
            "bedrooms_min": 3,
            "budget_rent": 12000,
            "geo_names": ["Bangsar"],
        }, "https://bubble.test")
        self.assertEqual(update["TransactionType"], ["Rent/Let"])
        self.assertEqual(update["bedroomsMin"], 3)
        self.assertEqual(update["budgetRent"], 12000)
        self.assertNotIn("Geo", update)

    @patch("app.get_plausible_listings")
    @patch("app.bubble")
    def test_matching_prompt_uses_structured_fields_not_ai_search_text(
        self, mocked_bubble, mocked_listings
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": []},
            {
                "TransactionType": ["Rent/Let"], "bedroomsMin": 3,
                "budgetRent": 12000, "AIsearchtext": "LEGACY GENERATED PROFILE",
            },
        ]
        mocked_listings.return_value = ([{
            "_id": "listing-1", "beds": 3, "priceRent": 11500,
            "AIsearchtext": "LEGACY GENERATED LISTING",
            "Description": "A real structured description",
        }], 1)
        model_response = SimpleNamespace(
            output_text=json.dumps({"recommendations": [], "customer_response": "No fit."}),
            usage=None,
        )
        with patch.object(app_module.client.responses, "create", return_value=model_response) as create:
            flow = app_module.match_lead("folio-1", "live", "message-1")
            while True:
                try:
                    next(flow)
                except StopIteration:
                    break
        prompt = create.call_args.kwargs["input"]
        self.assertIn('"bedrooms_min": 3.0', prompt)
        self.assertIn('"listing_id": "listing-1"', prompt)
        self.assertNotIn('"_id": "listing-1"', prompt)
        self.assertIn("A real structured description", prompt)
        self.assertNotIn("LEGACY GENERATED PROFILE", prompt)
        self.assertNotIn("LEGACY GENERATED LISTING", prompt)
        self.assertEqual(app_module.RANKING_MAX_OUTPUT_TOKENS, 3000)
        self.assertEqual(create.call_args.kwargs["max_output_tokens"], 3000)
        schema = create.call_args.kwargs["text"]["format"]["schema"]
        recommendations = schema["properties"]["recommendations"]
        self.assertEqual(recommendations["maxItems"], 7)
        self.assertEqual(
            recommendations["items"]["properties"]["reco_summary"]["maxLength"], 240
        )

    @patch("app.update_folio_items")
    @patch("app.create_folio_items", return_value=["folio-item-fallback"])
    @patch("app.get_plausible_listings")
    @patch("app.bubble")
    def test_all_invalid_model_ids_fall_back_to_structured_candidates(
        self, mocked_bubble, mocked_listings, mocked_create, mocked_update
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": []},
            {"TransactionType": ["Rent/Let"], "bedroomsMin": 3, "budgetRent": 15000},
        ]
        mocked_listings.return_value = ([
            {"_id": "listing-1", "beds": 3, "priceRent": 14000},
            {"_id": "listing-2", "beds": 3, "priceRent": 14500},
        ], 2)
        response = SimpleNamespace(
            output_text=json.dumps({
                "recommendations": [{
                    "listing_id": "invented-id", "reco_summary": "A fit."
                }],
                "customer_response": "Invented response.",
            }),
            usage=None,
        )
        with patch.object(app_module.client.responses, "create", return_value=response), \
                patch("builtins.print") as mocked_print:
            flow = app_module.match_lead("folio-1", "live", "message-1")
            while True:
                try:
                    next(flow)
                except StopIteration as completed:
                    answer = completed.value
                    break

        fallback = mocked_create.call_args.args[0]
        self.assertEqual(
            [item["listing_id"] for item in fallback],
            ["listing-1", "listing-2"],
        )
        self.assertIn("match your active search filters", answer)
        self.assertTrue(answer.recommendations_available)
        mocked_update.assert_called_once()
        logs = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("'invented-id'", logs)
        self.assertIn("valid_candidate_ids=['listing-1', 'listing-2']", logs)

    def test_geo_filter_accepts_listing_relationship_id(self):
        lead = {"Geo": ["geo-klcc"], "TransactionType": ["Rent/Let"]}
        listings = [
            {"_id": "klcc", "Geo": ["geo-klcc"], "priceRent": 12000},
            {"_id": "bangsar", "Geo": ["geo-bangsar"], "priceRent": 12000},
        ]
        self.assertEqual(
            [item["_id"] for item in app_module.shortlist_structured_listings(lead, listings)],
            ["klcc"],
        )

    @patch("app.update_folio_items")
    @patch("app.create_folio_items")
    @patch("app.get_plausible_listings")
    @patch("app.bubble")
    def test_truncated_matching_json_creates_nothing_and_is_not_successful(
        self, mocked_bubble, mocked_listings, mocked_create, mocked_update
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": []},
            {"TransactionType": ["Rent/Let"], "bedroomsMin": 3, "budgetRent": 15000},
        ]
        mocked_listings.return_value = ([{
            "_id": "listing-1", "beds": 3, "priceRent": 12000,
        }], 1)
        response = SimpleNamespace(
            output_text='{"recommendations":[{"listing_id":"listing-1","reco_summary":"unterminated',
            usage=None,
        )
        with patch.object(app_module.client.responses, "create", return_value=response):
            flow = app_module.match_lead("folio-1", "live", "message-1")
            while True:
                try:
                    next(flow)
                except StopIteration as completed:
                    result = completed.value
                    break
        self.assertIn("couldn’t prepare your recommendations", result)
        self.assertFalse(getattr(result, "recommendations_available", False))
        mocked_create.assert_not_called()
        mocked_update.assert_not_called()

    def test_selected_condos_are_limited_to_recommended_shortlist(self):
        state = set_recommended_condos(
            complete_state(), ["One Menerung", "Ken Bangsar", "The Loft"]
        )
        scope = listing_search_scope(
            state, ["One Menerung", "Unrelated Condo"]
        )
        self.assertEqual(scope, ["One Menerung"])

    def test_just_show_me_uses_full_shortlist_not_all_inventory(self):
        shortlist = ["One Menerung", "Ken Bangsar", "The Loft"]
        state = set_recommended_condos(complete_state(), shortlist)
        self.assertEqual(
            listing_search_scope(state, use_full_shortlist=True), shortlist
        )

    def test_listing_search_cannot_start_before_complete_brief_or_shortlist(self):
        self.assertEqual(
            listing_search_scope(empty_search_state(), use_full_shortlist=True), []
        )
        self.assertEqual(
            listing_search_scope(complete_state(), use_full_shortlist=True), []
        )

    def test_tool_schema_preserves_direct_information_tools(self):
        args = app_module.build_response_args("What is Ken Bangsar like?")
        tools = {tool.get("name"): tool for tool in args["tools"]}
        self.assertIn("advance_property_search", tools)
        self.assertIn("get_condo_info", tools)
        self.assertIn("get_property_details", tools)
        self.assertIn("match_lead", tools)
        self.assertIn("grounded listing-search scope", tools["advance_property_search"]["description"])

    def test_current_listing_requests_have_an_explicit_action_tool(self):
        args = app_module.build_response_args("ok show me the matches")
        tools = {tool.get("name"): tool for tool in args["tools"]}
        description = tools["match_lead"]["description"]
        self.assertIn("actual current Rentee listings", description)
        self.assertIn("asks to see properties", description)
        self.assertIn("zero-result response", description)
        self.assertIn("use the appropriate listing-search tool immediately", args["instructions"])
        self.assertIn("Do not describe an action instead of performing it", args["instructions"])

    def test_initial_tool_selection_is_bounded_for_compact_updates(self):
        args = app_module.build_response_args(
            "15k ringgit, furnished and move in asap"
        )
        self.assertEqual(args["reasoning"], {"effort": "low"})
        self.assertEqual(
            args["max_output_tokens"], app_module.INITIAL_MAX_OUTPUT_TOKENS
        )
        self.assertLessEqual(args["max_output_tokens"], 800)

    @patch("app.save_search_state")
    @patch("app.bubble")
    def test_show_matches_uses_entire_saved_condo_shortlist(
        self, mocked_bubble, _mocked_save
    ):
        stored = set_recommended_condos(
            complete_state(), ["One Menerung", "Ken Bangsar", "The Loft"]
        )
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]
        result = app_module.advance_property_search(
            "folio-1", "live", {"search_listings": True}
        )
        self.assertEqual(result["action"], "search_listings")
        self.assertEqual(
            result["scope"], ["One Menerung", "Ken Bangsar", "The Loft"]
        )

    @patch("app.save_search_state")
    @patch("app.bubble")
    def test_direct_listing_request_without_condo_shortlist_searches_saved_lead(
        self, mocked_bubble, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(empty_search_state())}
        ]
        result = app_module.advance_property_search(
            "folio-1", "live", {"search_listings": True}
        )
        self.assertEqual(result["action"], "search_listings")
        self.assertIsNone(result["scope"])

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["condo-one"])
    @patch("app.bubble")
    def test_named_condo_listing_request_is_constrained(
        self, mocked_bubble, _mocked_resolve, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(empty_search_state())}
        ]
        result = app_module.advance_property_search("folio-1", "live", {
            "preferred_condo_names": ["One Menerung"],
            "search_listings": True,
        })
        self.assertEqual(result["action"], "search_listings")
        self.assertEqual(result["scope"], ["One Menerung"])

    @patch("app.get_plausible_listings", return_value=([], 0))
    @patch("app.bubble")
    def test_zero_inventory_returns_truthful_answer_without_phantom_results(
        self, mocked_bubble, _mocked_listings
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": []},
            {"TransactionType": ["Rent/Let"], "bedroomsMin": 3, "budgetRent": 15000},
        ]
        with patch.object(app_module.client.responses, "create") as create:
            flow = app_module.match_lead("folio-1", "live", "message-1")
            while True:
                try:
                    next(flow)
                except StopIteration as completed:
                    answer = completed.value
                    break
        create.assert_not_called()
        lowered = answer.casefold()
        self.assertIn("don't have a suitable current property match", lowered)
        for phantom in ("which of these", "these properties", "options above", "which one"):
            self.assertNotIn(phantom, lowered)

    def test_property_search_skill_covers_area_intent_without_a_script(self):
        instructions = app_module.build_response_args(
            "I am looking to rent. Which area would you recommend?"
        )["instructions"]
        self.assertIn("# Property Search", instructions)
        self.assertIn("destinations", instructions)
        self.assertIn("not a questionnaire", instructions)
        self.assertNotIn("MUST", instructions)

    def test_property_search_skill_recommends_early_without_secondary_checklist(self):
        instructions = app_module.build_response_args(
            "15k rent a month ringgit, furnished or partially furnished"
        )["instructions"]
        self.assertIn("Recommend early", instructions)
        self.assertIn("generally\nenough", instructions)
        self.assertIn("current turn", instructions)
        self.assertIn("should not normally delay", instructions)
        self.assertIn("never\na checklist", instructions)
        self.assertNotIn("I'll pull", instructions)

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["geo-bangsar"])
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_budget_followup_recommends_now_and_retains_furnishing(
        self, mocked_bubble, mocked_recommend, _mocked_resolve, _mocked_save
    ):
        stored = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": ["rent"], "bedroom_requirement": "3",
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]
        mocked_recommend.return_value = (
            [{"condo_name": "One Menerung", "reason": "Strong fit"}],
            "At this budget, One Menerung is a strong place to start.",
        )
        result = app_module.advance_property_search("folio-1", "live", {
            "transaction_type": "rent",
            "budget_rent": 15000,
            "preference_notes": ["Furnished or partially furnished"],
            "recommend_condos": True,
        })
        self.assertEqual(result["action"], "condo_shortlist")
        self.assertIn(
            "Furnished or partially furnished", result["state"]["preference_notes"]
        )
        self.assertEqual(_mocked_save.call_args.args[3]["budgetRent"], 15000)

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["geo-bangsar"])
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_clear_area_refinement_continues_recommending_without_questions(
        self, mocked_bubble, mocked_recommend, _mocked_resolve, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(complete_state())}
        ]
        mocked_recommend.return_value = (
            [{"condo_name": "One Menerung", "reason": "Bangsar proper"}],
            "I'll keep this to Bangsar proper.",
        )
        result = app_module.advance_property_search("folio-1", "live", {
            "geo_names": ["Bangsar"],
            "preference_notes": ["Exclude Bangsar South", "Move in as soon as possible"],
            "recommend_condos": True,
        })
        self.assertEqual(result["action"], "condo_shortlist")
        self.assertEqual(result["state"]["areas"], ["Bangsar"])
        self.assertIn("Exclude Bangsar South", result["state"]["preference_notes"])

    def test_personalised_search_routing_prompt_captures_named_area(self):
        args = app_module.build_response_args("I want to rent in Bangsar")
        tool = next(
            item for item in args["tools"]
            if item.get("name") == "advance_property_search"
        )
        self.assertIn("Save new or refined search requirements", tool["description"])
        self.assertIn("constrains listings", tool["description"])

    def test_general_area_question_is_excluded_from_personalised_search(self):
        instructions = app_module.build_response_args(
            "What is Bangsar like?"
        )["instructions"]
        self.assertIn("neighbourhood", instructions)
        self.assertIn("Answer the actual question first", instructions)

    def test_named_condo_question_still_routes_to_condo_knowledge(self):
        args = app_module.build_response_args("What is Ken Bangsar like?")
        instructions = args["instructions"]
        condo_tool = next(
            item for item in args["tools"] if item.get("name") == "get_condo_info"
        )
        self.assertIn("Use `get_condo_info` for named developments", instructions)
        self.assertIn("condo facts", condo_tool["description"])

    @patch("app.requests.patch")
    @patch("app.bubble")
    def test_sparse_search_updates_structured_lead_and_asks_model_question(
        self, mocked_bubble, mocked_patch
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": ""}
        ]
        mocked_patch.return_value.raise_for_status.return_value = None
        result = app_module.advance_property_search("folio-1", "live", {
            "bedrooms_min": 4,
            "question": "Are you looking to rent or buy?",
        })
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["text"], "Are you looking to rent or buy?")
        payload = mocked_patch.call_args.kwargs["json"]
        self.assertIn("searchBriefJSON", payload)
        self.assertEqual(payload["bedroomsMin"], 4)

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["geo-bangsar"])
    @patch("app.bubble")
    def test_initial_core_requirements_capture_and_ask_only_for_budget(
        self, mocked_bubble, _mocked_resolve, mocked_save
    ):
        mocked_bubble.side_effect = [{"lead": "lead-1"}, {"searchBriefJSON": ""}]
        result = app_module.advance_property_search("folio-1", "live", {
            "transaction_type": "rent",
            "bedrooms_min": 3,
            "geo_names": ["Bangsar"],
            "question": "What's your monthly rental budget?",
        })
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["text"], "What's your monthly rental budget?")
        lead_fields = mocked_save.call_args.args[3]
        self.assertEqual(lead_fields["TransactionType"], ["Rent/Let"])
        self.assertEqual(lead_fields["bedroomsMin"], 3)
        self.assertEqual(lead_fields["Geo"], ["geo-bangsar"])

    @patch("app.requests.patch")
    @patch("app.get_named_object_ids", return_value=["geo-bangsar"])
    @patch("app.bubble")
    def test_multiple_structured_requirements_are_persisted_without_llm_rewrite(
        self, mocked_bubble, _mocked_resolve, mocked_patch
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": ""}
        ]
        mocked_patch.return_value.raise_for_status.return_value = None

        with patch("app.recommend_condos_for_search",
            return_value=([{"condo_name": "One Menerung", "reason": "fit"}], "A fit"),
        ) as mocked_recommend:
            result = app_module.advance_property_search("folio-1", "live", {
                "transaction_type": "rent",
                "geo_names": ["Bangsar"],
                "property_types": ["Condo"],
                "bedrooms_min": 4,
                "budget_rent": 12000,
                "recommend_condos": True,
            })

        self.assertEqual(result["state"]["bedroom_requirement"], "4")
        mocked_recommend.assert_called_once()
        payload = mocked_patch.call_args.kwargs["json"]
        self.assertEqual(payload["TransactionType"], ["Rent/Let"])
        self.assertEqual(payload["bedroomsMin"], 4)
        self.assertEqual(payload["budgetRent"], 12000)
        self.assertEqual(payload["Geo"], ["geo-bangsar"])

    def test_derived_profile_text_remains_available_for_bubble_display(self):
        text = app_module.search_state_to_requirements_text(complete_state())
        self.assertIn("Areas: Bangsar", text)
        self.assertIn("Property types: Condo", text)
        self.assertIn("Bedrooms: exactly 4 bedrooms", text)
        self.assertIn("Budget: maximum RM12,000", text)
        self.assertIn("Other requirements: furnished, balcony", text)
        self.assertIn("Ordered priorities: location, bedrooms, balcony", text)

    @patch("app.requests.patch")
    def test_structured_save_logs_bubble_error_body(self, mocked_patch):
        response = mocked_patch.return_value
        response.status_code = 400
        response.text = '{"error":"invalid field"}'
        response.raise_for_status.side_effect = app_module.requests.HTTPError("bad request")

        with patch("builtins.print") as mocked_print, self.assertRaises(
            app_module.requests.HTTPError
        ):
            app_module.save_search_state("lead-1", empty_search_state(), "https://bubble.test")

        self.assertTrue(any(
            'HTTP 400 body={"error":"invalid field"}' in str(call)
            for call in mocked_print.call_args_list
        ))

    @patch("app.save_search_state")
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_complete_brief_recommends_condos_before_inventory(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": json.dumps(complete_state())}
        ]
        mocked_recommend.return_value = (
            [{"condo_name": "One Menerung", "reason": "Fits the brief"}],
            "I'd focus on One Menerung.",
        )
        result = app_module.advance_property_search(
            "folio-1", "live", {"recommend_condos": True}
        )
        self.assertEqual(result["action"], "condo_shortlist")
        self.assertIn("Which would you like to explore?", result["text"])

    def test_listing_filter_resolves_bubble_condo_relationship(self):
        listing = {"_id": "listing-1", "condo": "condo-id"}
        with patch("app.bubble", return_value={"name": "One Menerung"}):
            self.assertTrue(app_module._listing_is_in_condo_scope(
                listing, ["One Menerung"], "https://bubble.test", {}
            ))
            self.assertFalse(app_module._listing_is_in_condo_scope(
                listing, ["Ken Bangsar"], "https://bubble.test", {}
            ))

    def test_active_area_modes_preserve_unchanged_requirements(self):
        active = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": ["rent"], "bedroom_requirement": "3",
            "budget_requirement": "15000",
        })
        active["recommended_condos"] = ["One Menerung"]
        active["selected_condos"] = ["One Menerung"]
        replaced = app_module.apply_active_search_update(active, {
            "geo_names": ["KLCC"], "area_update_mode": "replace",
        })
        self.assertEqual(replaced["areas"], ["KLCC"])
        self.assertEqual(replaced["bedroom_requirement"], "3")
        self.assertEqual(replaced["budget_requirement"], "15000")
        self.assertEqual(replaced["property_types"], ["rent"])

        added = app_module.apply_active_search_update(active, {
            "geo_names": ["KLCC"], "area_update_mode": "add",
        })
        self.assertEqual(added["areas"], ["Bangsar", "KLCC"])
        removed = app_module.apply_active_search_update(added, {
            "geo_names": ["Bangsar"], "area_update_mode": "remove",
        })
        self.assertEqual(removed["areas"], ["KLCC"])

    def test_active_scalar_and_multiple_changes_preserve_other_fields(self):
        active = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": ["rent"], "bedroom_requirement": "3",
            "budget_requirement": "15000",
        })
        active["recommended_condos"] = ["One Menerung"]
        active["selected_condos"] = ["One Menerung"]
        bedrooms = app_module.apply_active_search_update(
            active, {"bedrooms_min": 4}
        )
        self.assertEqual(bedrooms["areas"], ["Bangsar"])
        self.assertEqual(bedrooms["bedroom_requirement"], "4")
        self.assertEqual(bedrooms["budget_requirement"], "15000")
        self.assertEqual(bedrooms["selected_condos"], ["One Menerung"])
        budget = app_module.apply_active_search_update(
            active, {"budget_rent": 18000}
        )
        self.assertEqual(budget["bedroom_requirement"], "3")
        self.assertEqual(budget["budget_requirement"], "18000")
        multiple = app_module.apply_active_search_update(active, {
            "geo_names": ["KLCC"], "area_update_mode": "replace",
            "bedrooms_min": 2, "budget_rent": 12000,
        })
        self.assertEqual(
            (multiple["areas"], multiple["bedroom_requirement"],
             multiple["budget_requirement"], multiple["property_types"]),
            (["KLCC"], "2", "12000", ["rent"]),
        )

    @patch("app.save_search_state")
    @patch("app.get_named_object_ids", return_value=["geo-klcc"])
    @patch("app.bubble")
    def test_klcc_followup_keeps_cumulative_bangsar_but_replaces_active_area(
        self, mocked_bubble, _resolve, mocked_save
    ):
        cumulative = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": ["rent"], "bedroom_requirement": "3",
            "budget_requirement": "15000",
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {"_id": "lead-1", "Geo": ["geo-bangsar"],
             "searchBriefJSON": dump_search_state(cumulative),
             "searchActive": dump_search_state(cumulative)},
        ]
        result = app_module.advance_property_search("folio-1", "live", {
            "geo_names": ["KLCC"], "area_update_mode": "replace",
            "search_listings": True,
        })
        self.assertEqual(result["state"]["areas"], ["Bangsar", "KLCC"])
        self.assertEqual(result["active_state"]["areas"], ["KLCC"])
        self.assertEqual(result["active_state"]["bedroom_requirement"], "3")
        self.assertEqual(result["active_state"]["budget_requirement"], "15000")
        lead_fields = mocked_save.call_args.args[3]
        self.assertEqual(lead_fields["Geo"], ["geo-bangsar", "geo-klcc"])

    @patch("app.get_named_object_ids")
    def test_active_filters_ignore_cumulative_geo_and_preferred_condos(self, resolve):
        resolve.side_effect = lambda _base, object_type, names: (
            ["geo-klcc"] if object_type == "geo" and names == ["KLCC"] else []
        )
        active = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["KLCC"],
            "property_types": ["rent"], "bedroom_requirement": "3",
            "budget_requirement": "15000",
        })
        lead = {
            "Geo": ["geo-bangsar", "geo-klcc"],
            "preferredCondos": ["condo-one-menerung"],
            "searchActive": dump_search_state(active),
        }
        filtered = app_module.lead_with_active_search_filters(
            lead, "https://bubble.test"
        )
        requirements = app_module.structured_lead_requirements(filtered)
        self.assertEqual(requirements["geo_ids"], ["geo-klcc"])
        self.assertEqual(requirements["preferred_condo_ids"], [])
        self.assertEqual(requirements["bedrooms_min"], 3)
        self.assertEqual(requirements["budget_rent"], 15000)

    def test_missing_and_malformed_active_state_fall_back_without_crashing(self):
        cumulative = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "bedroom_requirement": "3", "budget_requirement": "15000",
        })
        self.assertEqual(
            app_module.load_active_search_state({
                "searchBriefJSON": dump_search_state(cumulative)
            })["areas"],
            ["Bangsar"],
        )
        with patch("builtins.print") as mocked_print:
            fallback = app_module.load_active_search_state({
                "searchActive": "{bad json",
                "searchBriefJSON": dump_search_state(cumulative),
            })
        self.assertEqual(fallback["areas"], ["Bangsar"])
        self.assertIn(
            "invalid JSON", "\n".join(str(call) for call in mocked_print.call_args_list)
        )

    def test_explicit_new_search_resets_active_not_cumulative_state(self):
        active = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "bedroom_requirement": "3", "budget_requirement": "15000",
        })
        reset = app_module.apply_active_search_update(active, {
            "new_search": True, "geo_names": ["KLCC"],
            "area_update_mode": "replace",
        })
        self.assertEqual(reset["areas"], ["KLCC"])
        self.assertEqual(reset["bedroom_requirement"], "")
        self.assertEqual(reset["budget_requirement"], "")

if __name__ == "__main__":
    unittest.main()
