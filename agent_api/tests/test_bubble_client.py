import logging
import os
import unittest
from unittest.mock import Mock, patch

import requests

from agent_api.bubble_client import BubbleClient, BubbleUnavailable


class BubbleClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret-test-token"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_default_uses_verified_live_api_root(self):
        self.assertEqual(BubbleClient().base, "https://www.rentee.asia/api/1.1")

    @patch("agent_api.bubble_client.requests.Session.request")
    def test_create_accepts_standard_top_level_id(self, request):
        response = Mock(status_code=201)
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "success", "id": "session-1"}
        request.return_value = response

        self.assertEqual(BubbleClient().create("AgentSession", {"user": "u1"}), "session-1")
        self.assertEqual(request.call_args.args[:2], ("POST", "https://www.rentee.asia/api/1.1/obj/AgentSession"))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer secret-test-token")

    @patch("agent_api.bubble_client.requests.Session.request")
    def test_create_accepts_wrapped_id(self, request):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": {"id": "session-2"}}
        request.return_value = response
        self.assertEqual(BubbleClient().create("AgentSession", {}), "session-2")

    @patch("agent_api.bubble_client.requests.Session.request")
    def test_failure_log_is_diagnostic_and_redacted(self, request):
        response = Mock(status_code=401)
        error = requests.HTTPError("secret-test-token raw-sensitive-body", response=response)
        response.raise_for_status.side_effect = error
        request.return_value = response

        with self.assertLogs("agent_api.bubble_client", logging.WARNING) as captured:
            with self.assertRaises(BubbleUnavailable):
                BubbleClient().create("AgentSession", {"token_hash": "sensitive-hash"})
        rendered = " ".join(captured.output)
        self.assertIn("operation=create object_type=AgentSession status=401 reason=http_error", rendered)
        self.assertNotIn("secret-test-token", rendered)
        self.assertNotIn("sensitive-hash", rendered)
        self.assertNotIn("raw-sensitive-body", rendered)

    @patch("agent_api.bubble_client.requests.Session.request")
    def test_invalid_success_body_is_reported_as_bubble_unavailable(self, request):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("contains response contents")
        request.return_value = response
        with self.assertLogs("agent_api.bubble_client", logging.WARNING) as captured:
            with self.assertRaises(BubbleUnavailable):
                BubbleClient().search("AgentSession", [])
        self.assertIn("operation=search object_type=AgentSession status=200 reason=invalid_json", " ".join(captured.output))
        self.assertNotIn("contains response contents", " ".join(captured.output))

    @patch("agent_api.bubble_client.requests.Session.request")
    def test_many_deduplicates_ids_and_uses_bubble_in_constraint(self, request):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"response":{"results":[{"_id":"c1"},{"_id":"c2"}],"remaining":0}}
        request.return_value = response
        rows=BubbleClient().many("condo",["c1","c2","c1"])
        self.assertEqual([row["_id"] for row in rows],["c1","c2"])
        constraints=request.call_args.kwargs["params"]["constraints"]
        self.assertEqual(constraints,'[{"key":"_id","constraint_type":"in","value":["c1","c2"]}]')

    @patch("agent_api.bubble_client.BubbleClient.search")
    def test_all_without_maximum_follows_every_page(self, search):
        search.side_effect=[([{"_id":str(i)} for i in range(100)],150),
                            ([{"_id":str(i)} for i in range(100,200)],50),
                            ([{"_id":str(i)} for i in range(200,250)],0)]
        rows=BubbleClient().all("match",[],None)
        self.assertEqual(len(rows),250)
        self.assertEqual([call.args[3] for call in search.call_args_list],[0,100,200])


if __name__ == "__main__":
    unittest.main()
