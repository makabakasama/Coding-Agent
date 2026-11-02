"""Direct checks for the small persisted task list, without execution."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from tests import test_tasks as fixtures


class TaskListTest(unittest.TestCase):
    setUp = fixtures.TaskIntegrationTest.setUp
    git = fixtures.TaskIntegrationTest.git

    def test_latest_fifty_and_http_listing(self):
        template = self.service.create(fixtures.request_contract())
        for index in range(51):
            stamp = (datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
            self.service.store.create(lambda task_id: {**template, "taskId": task_id, "createdAt": stamp})
        with patch.object(self.service.preparation, "query", side_effect=AssertionError("List must not prepare or preflight")):
            with fixtures.serving(self.service) as port:
                status, _, value = fixtures.http(port, "GET", "/api/tasks")
        self.assertEqual(200, status)
        self.assertEqual(50, len(value["tasks"]))
        self.assertGreater(value["tasks"][0]["createdAt"], value["tasks"][-1]["createdAt"])
        self.assertEqual({"taskId", "createdAt", "projectId", "current", "executionStatus", "resultAvailable"}, set(value["tasks"][0]))
        self.assertTrue(all(t["executionStatus"] == "not_started" and not t["resultAvailable"] for t in value["tasks"]))

    def test_empty_and_unknown_execution_with_existing_result(self):
        self.assertEqual([], self.service.list())
        record = self.service.create(fixtures.request_contract())
        directory = self.data / f"task_{record['taskId']}/execution"
        directory.mkdir()
        self.assertEqual("unknown", self.service.list()[0]["executionStatus"])
        (directory / "state.json").write_text("{broken")
        (directory / "result.json").write_text("{}")
        item = self.service.list()[0]
        self.assertEqual("unknown", item["executionStatus"])
        self.assertTrue(item["resultAvailable"])


if __name__ == "__main__":
    unittest.main()
