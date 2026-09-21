import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import development_resolver as resolver


GEOS = [
    {"_id": "geo-bangsar", "Name": "Bangsar"},
    {"_id": "geo-mk", "Name": "Mont Kiara"},
]
DEVELOPMENTS = [
    {"_id": "dev-one", "Name": "One Menerung", "Geo": "geo-bangsar"},
]


class DevelopmentResolverTests(unittest.TestCase):
    def test_existing_development_returns_without_web_verification(self):
        with patch.object(resolver, "verify_development_candidate") as verify:
            result = resolver.resolve_or_create_development(
                "one menerung", {}, development_records=DEVELOPMENTS,
                geo_records=GEOS,
            )
        verify.assert_not_called()
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["action"], "existing")
        self.assertEqual(result["development_id"], "dev-one")
        self.assertEqual(result["geo_id"], "geo-bangsar")

    def test_verbose_verified_geo_creates_with_existing_geo(self):
        verification = {
            "status": "verified", "canonical_name": "Ceriaan Kiara",
            "geo_name": "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur",
            "verification_url": "https://example.com/ceriaan",
            "confidence": 0.90, "reason": "credible_match",
        }
        with patch.object(resolver, "verify_development_candidate",
                          return_value=verification), \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(resolver.rentee_app, "_bubble_create",
                          return_value="dev-ceriaan") as create:
            result = resolver.resolve_or_create_development(
                "Ceriaan Kiara", {}, development_records=DEVELOPMENTS,
                geo_records=GEOS,
            )
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["geo_id"], "geo-mk")
        self.assertEqual(create.call_args.args[2], {
            "name": "Ceriaan Kiara", "Geo": "geo-mk",
            "verification_status": "Verified", "source": "WhatsApp Import",
            "verification_url": "https://example.com/ceriaan",
        })
        self.assertNotIn("Name", create.call_args.args[2])

    def test_verified_development_uses_single_geo_fallback_match(self):
        verification = {
            "status": "verified", "canonical_name": "Lakeview Residences",
            "geo_name": "Lakeview Township",
            "verification_url": "https://example.com/lakeview",
            "confidence": 0.96, "reason": "credible_match",
        }
        fallback_geo = {
            "matched": True, "id": "geo-bangsar", "name": "Bangsar",
            "record": GEOS[0], "method": "normalized_exact",
        }
        geo_verifier = unittest.mock.Mock(return_value=[fallback_geo])
        with patch.object(resolver, "verify_development_candidate",
                          return_value=verification), \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(resolver.rentee_app, "_bubble_create",
                          return_value="dev-lakeview") as create:
            result = resolver.resolve_or_create_development(
                "Lakeview", {"raw_text": "Lakeview listing"},
                development_records=DEVELOPMENTS, geo_records=GEOS,
                geo_verifier=geo_verifier,
            )
        geo_verifier.assert_called_once_with(
            "Lakeview Township", GEOS, {"raw_text": "Lakeview listing"},
            single=True, bubble_env="live",
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["geo_id"], "geo-bangsar")
        self.assertEqual(create.call_args.args[2]["Geo"], "geo-bangsar")

    def test_verified_development_stays_geo_unresolved_without_one_fallback(self):
        verification = {
            "status": "verified", "canonical_name": "Lakeview Residences",
            "geo_name": "Unknown Township",
            "verification_url": "https://example.com/lakeview",
            "confidence": 0.96, "reason": "credible_match",
        }
        geo_verifier = unittest.mock.Mock(return_value=[])
        with patch.object(resolver, "verify_development_candidate",
                          return_value=verification), \
             patch.object(resolver.rentee_app, "_bubble_create") as create:
            result = resolver.resolve_or_create_development(
                "Lakeview", {}, development_records=DEVELOPMENTS,
                geo_records=GEOS, geo_verifier=geo_verifier,
            )
        self.assertEqual((result["status"], result["reason"]),
                         ("not_found", "geo_unresolved"))
        create.assert_not_called()

    def test_geo_variants_normalize_and_duplicate_keys_are_ambiguous(self):
        variants = [
            "Mont Kiara", "Mont'Kiara", "Mont' Kiara", "Mont’ Kiara",
            "Mont' Kiara, Kuala Lumpur",
            "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur",
            "Mont Kiara, Kuala Lumpur, Malaysia",
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(resolver.resolve_geo_name(variant, GEOS)["id"], "geo-mk")
        duplicate_geos = GEOS + [{"_id": "geo-mk-2", "Name": "Mont'Kiara"}]
        ambiguous = resolver.resolve_geo_name(
            "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur", duplicate_geos
        )
        self.assertFalse(ambiguous["matched"])
        self.assertEqual(ambiguous["reason"], "ambiguous")

    def test_ambiguous_and_error_outcomes_do_not_create(self):
        for outcome in (
            {"status": "ambiguous", "reason": "multiple_plausible_candidates"},
            {"status": "error", "reason": "invalid_json"},
        ):
            with self.subTest(status=outcome["status"]), \
                 patch.object(resolver, "verify_development_candidate",
                              return_value=outcome), \
                 patch.object(resolver.rentee_app, "_bubble_create") as create:
                result = resolver.resolve_or_create_development(
                    "Unknown", {}, development_records=DEVELOPMENTS,
                    geo_records=GEOS,
                )
            self.assertEqual(result["status"], outcome["status"])
            create.assert_not_called()

    def test_invalid_json_and_missing_url_are_errors(self):
        with patch.object(
            resolver.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text="not-json"),
        ):
            invalid = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual((invalid["status"], invalid["reason"]),
                         ("error", "invalid_json"))

        missing_url = {
            "status": "verified", "canonical_name": "Inspirasi Mont Kiara",
            "geo_name": "Mont Kiara", "confidence": 0.96,
            "reason": "credible_match",
        }
        with patch.object(
            resolver.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(missing_url)),
        ):
            invalid = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual((invalid["status"], invalid["reason"]),
                         ("error", "missing_verification_url"))

    def test_valid_not_found_remains_distinct_from_error(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.1,
            "reason": "no_credible_property_match",
        }
        with patch.object(
            resolver.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ) as create:
            result = resolver.verify_development_candidate("Not Real", {})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["reason"], "no_credible_property_match")
        create.assert_called_once()

    def test_verifier_recovers_fenced_json_without_retry(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.1,
            "reason": "no_credible_property_match",
        }
        response = SimpleNamespace(
            output_text=f"Here is the result:\n```json\n{json.dumps(output)}\n```"
        )
        with patch.object(
            resolver.rentee_app.client.responses, "create", return_value=response,
        ) as create:
            result = resolver.verify_development_candidate("Not Real", {})
        self.assertEqual(result["status"], "not_found")
        create.assert_called_once()

    def test_verifier_retries_invalid_json_once_and_succeeds(self):
        output = {
            "status": "verified", "canonical_name": "Ceriaan Kiara",
            "geo_name": "Mont Kiara",
            "verification_url": "https://example.com/ceriaan",
            "confidence": 0.96, "reason": "credible_match",
        }
        with patch.object(
            resolver.rentee_app.client.responses, "create",
            side_effect=[
                SimpleNamespace(
                    output_text='{"status":"verified","canonical_name":"Ceriaan',
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                SimpleNamespace(output_text=json.dumps(output)),
            ],
        ) as create, patch("builtins.print") as log:
            result = resolver.verify_development_candidate("Not Real", {})
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["canonical_name"], "Ceriaan Kiara")
        self.assertEqual(create.call_count, 2)
        self.assertIn(
            "Return ONLY one complete JSON object matching the required schema, "
            "with no prose or markdown.",
            create.call_args_list[1].kwargs["input"],
        )
        self.assertEqual(
            create.call_args_list[0].kwargs["max_output_tokens"],
            resolver.DEVELOPMENT_VERIFIER_MAX_OUTPUT_TOKENS,
        )
        rendered = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn("status=retry", rendered)
        self.assertIn("incomplete_details={'reason': 'max_output_tokens'}", rendered)
        self.assertIn("status=retry_succeeded", rendered)

    def test_verifier_returns_invalid_json_after_single_failed_retry(self):
        with patch.object(
            resolver.rentee_app.client.responses, "create",
            side_effect=[SimpleNamespace(output_text="bad first"),
                         SimpleNamespace(output_text="bad second")],
        ) as create, patch("builtins.print") as log:
            result = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual((result["status"], result["reason"]),
                         ("error", "invalid_json"))
        self.assertEqual(create.call_count, 2)
        rendered = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn("status=retry reason='invalid_json'", rendered)
        self.assertIn("status=retry_failed reason='invalid_json'", rendered)
        self.assertIn("output_preview='bad second'", rendered)

    def test_create_race_requeries_once_and_reuses(self):
        geo = resolver.resolve_geo_name("Mont Kiara", GEOS)
        raced = {"_id": "dev-raced", "Name": "Residensi Sefina", "Geo": "geo-mk"}
        with patch.object(resolver, "_fresh_development_records",
                          side_effect=[[], [raced]]) as fresh, \
             patch.object(resolver.rentee_app, "_bubble_create",
                          side_effect=RuntimeError("duplicate")) as create:
            result = resolver.create_verified_development(
                "Residensi Sefina", geo, "https://example.com/sefina"
            )
        self.assertEqual(result["id"], "dev-raced")
        self.assertEqual(fresh.call_count, 2)
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
