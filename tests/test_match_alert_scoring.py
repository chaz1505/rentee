import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import whatsapp_importer as importer


class MatchAlertScoringTests(unittest.TestCase):
    def _run(self, scores, *, owners=None, created_type="lead", existing=None,
             max_recipients="20", send_side_effect=None):
        owners = owners or {record_id: f"owner-{record_id}" for record_id in scores}
        matches = [
            {"_id": record_id, "owner": owners[record_id],
             "TransactionType": ["Buy/Sell"]}
            for record_id in scores
        ]
        created = {"_id": "lead-new" if created_type == "lead" else "listing-new",
                   "owner": "importer", "TransactionType": ["Buy/Sell"]}
        scoring = {
            record_id: {"score": score, "reason": f"reason {score}"}
            for record_id, score in scores.items() if score is not None
        }
        existing = existing or []
        records = iter(existing + [[] for _ in matches])

        def bubble_records(*_args, **_kwargs):
            return next(records)

        send_patch = patch.object(importer, "maybe_send_match_alert")
        with patch.dict(os.environ, {
            "MATCH_ALERT_MIN_SCORE": "80",
            "MATCH_ALERT_MAX_RECIPIENTS": max_recipients,
        }, clear=False), patch.object(
            importer.rentee_app, "_bubble_records", side_effect=bubble_records
        ), patch.object(
            importer.rentee_app, "_bubble_create",
            side_effect=[f"match-{index}" for index in range(len(matches))],
        ), send_patch as send:
            if send_side_effect is None:
                send.return_value = True
            else:
                send.side_effect = send_side_effect
            importer.create_missing_match_records(
                created_type, created["_id"], matches, "live",
                created_record=created, match_scores=scoring,
            )
        return send

    def test_score_threshold_is_inclusive_and_invalid_scores_do_not_alert(self):
        send = self._run({"high": 95, "edge": 80, "low": 79, "missing": None})
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            [call.kwargs["grouped_matches"][0]["score"] for call in send.call_args_list],
            [95, 80],
        )

    def test_multiple_matches_for_one_recipient_are_consolidated_and_score_sorted(self):
        owners = {"a": "caroline", "b": "caroline", "c": "caroline"}
        send = self._run({"a": 80, "b": 95, "c": 88}, owners=owners)
        send.assert_called_once()
        grouped = send.call_args.kwargs["grouped_matches"]
        self.assertEqual([item["score"] for item in grouped], [95, 88, 80])

    def test_recipient_cap_selects_twenty_highest_ranked_groups(self):
        scores = {f"listing-{index}": 80 + index for index in range(21)}
        send = self._run(scores, max_recipients="20")
        self.assertEqual(send.call_count, 20)
        selected = [
            call.kwargs["grouped_matches"][0]["score"]
            for call in send.call_args_list
        ]
        self.assertEqual(selected, list(range(100, 80, -1)))

    def test_failure_for_one_recipient_does_not_block_the_next(self):
        send = self._run(
            {"best": 95, "next": 90},
            send_side_effect=[RuntimeError("Meta unavailable"), True],
        )
        self.assertEqual(send.call_count, 2)

    def test_existing_alert_sent_match_is_not_reprocessed(self):
        send = self._run(
            {"listing": 95},
            existing=[[{"_id": "match-existing", "alert_sent": True}]],
        )
        send.assert_not_called()

    def test_listing_import_uses_same_score_selection(self):
        send = self._run({"lead-existing": 95}, created_type="listing")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "listing")

    def test_display_limit_does_not_limit_alert_candidates(self):
        scores = {f"listing-{index}": 90 - index for index in range(5)}
        owners = {record_id: f"owner-{record_id}" for record_id in scores}
        with patch.dict(os.environ, {"MATCH_DISPLAY_LIMIT": "1"}, clear=False):
            rendered = importer.format_import_matches(
                "lead", {"_id": "lead-new"},
                [{"_id": record_id, "owner": owners[record_id]} for record_id in scores],
                match_scores={record_id: {"score": score, "reason": "fit"}
                              for record_id, score in scores.items()},
            )
        self.assertIn("showing top 1", rendered)
        send = self._run(scores, owners=owners)
        self.assertEqual(send.call_count, 5)

    def test_invalid_configuration_falls_back_safely(self):
        with patch.dict(os.environ, {
            "MATCH_ALERT_MIN_SCORE": "invalid",
            "MATCH_ALERT_MAX_RECIPIENTS": "-1",
        }, clear=False):
            self.assertEqual(importer._match_alert_min_score(), 80)
            self.assertEqual(importer._match_alert_max_recipients(), 20)

    def test_malformed_score_never_triggers_alert(self):
        match = {"_id": "listing", "owner": "caroline"}
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="match-1"), \
             patch.object(importer, "maybe_send_match_alert") as send:
            importer.create_missing_match_records(
                "lead", "lead-new", [match], "live",
                created_record={"owner": "linda"},
                match_scores={"listing": {"score": "95", "reason": "fit"}},
            )
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
