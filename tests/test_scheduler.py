import unittest
from datetime import datetime, timedelta, timezone

from scheduler import generate_candidate_slots, optimise_schedule


UTC = timezone.utc


def at(hour, minute=0):
    return datetime(2026, 8, 29, hour, minute, tzinfo=UTC)


def candidate(request_id, start, priority=0, duration=30):
    return {
        "viewing_request_id": request_id,
        "listing_id": f"listing-{request_id}",
        "listing_name": f"Listing {request_id}",
        "start": start,
        "end": start + timedelta(minutes=duration),
        "priority": priority,
    }


class SchedulerTests(unittest.TestCase):
    def optimise(self, candidates, buffer=15):
        return optimise_schedule(candidates, at(10), at(14), buffer)

    def test_schedules_two_compatible_viewings(self):
        result = self.optimise([candidate("a", at(10)), candidate("b", at(11))])
        self.assertEqual(result["scheduled_count"], 2)

    def test_rejects_overlapping_appointments(self):
        result = self.optimise([
            candidate("a", at(10), duration=60), candidate("b", at(10, 30))
        ], buffer=0)
        self.assertEqual(result["scheduled_count"], 1)

    def test_respects_travel_buffer(self):
        result = self.optimise([
            candidate("a", at(10)), candidate("b", at(10, 40))
        ], buffer=15)
        self.assertEqual(result["scheduled_count"], 1)

    def test_never_schedules_one_request_twice(self):
        result = self.optimise([
            candidate("a", at(10)), candidate("a", at(11))
        ])
        self.assertEqual(result["scheduled_count"], 1)

    def test_maximises_count_before_priority(self):
        result = self.optimise([
            candidate("high", at(10), priority=100, duration=120),
            candidate("a", at(10), priority=1),
            candidate("b", at(11), priority=1),
        ], buffer=0)
        self.assertEqual(result["scheduled_count"], 2)
        self.assertEqual(
            {item["viewing_request_id"] for item in result["appointments"]},
            {"a", "b"},
        )

    def test_uses_priority_to_break_count_ties(self):
        result = self.optimise([
            candidate("low", at(10), priority=1),
            candidate("high", at(10), priority=5),
        ], buffer=0)
        self.assertEqual(result["appointments"][0]["viewing_request_id"], "high")

    def test_minimises_idle_time_after_count_and_priority(self):
        result = self.optimise([
            candidate("a", at(10)),
            candidate("b", at(11)),
            candidate("b", at(12)),
        ], buffer=0)
        self.assertEqual(result["idle_minutes"], 30)
        self.assertEqual(result["appointments"][1]["start"], at(11))

    def test_respects_tenant_availability(self):
        result = self.optimise([
            candidate("outside", at(9, 30)), candidate("inside", at(10))
        ])
        self.assertEqual(
            [item["viewing_request_id"] for item in result["appointments"]],
            ["inside"],
        )

    def test_candidate_generation_respects_agent_availability(self):
        slots = generate_candidate_slots(
            "request", "listing", "Listing", at(10), at(12), at(9), at(13)
        )
        self.assertEqual([slot["start"] for slot in slots], [
            at(10), at(10, 30), at(11), at(11, 30)
        ])
        self.assertTrue(all(slot["end"] <= at(12) for slot in slots))

    def test_one_request_with_multiple_slots_uses_compatible_slot(self):
        result = self.optimise([
            candidate("a", at(10)),
            candidate("a", at(11)),
            candidate("b", at(10)),
        ], buffer=0)
        self.assertEqual(result["scheduled_count"], 2)
        selected = {item["viewing_request_id"]: item for item in result["appointments"]}
        self.assertEqual(selected["a"]["start"], at(11))

    def test_request_with_no_availability_does_not_fail(self):
        result = self.optimise([candidate("available", at(10))])
        self.assertEqual(result["scheduled_count"], 1)
        self.assertNotIn("missing", {
            item["viewing_request_id"] for item in result["appointments"]
        })


if __name__ == "__main__":
    unittest.main()
