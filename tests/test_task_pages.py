import unittest
import json
from unittest.mock import AsyncMock, patch

from a2a.helpers import new_text_message, new_text_part
from a2a.types import Task, TaskStatus, TaskState, Artifact

from agent_bridge.client import BridgeClient, _task_result


class TaskPagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.task = Task(id="task", context_id="context", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                         history=[new_text_message("prompt")],
                         artifacts=[Artifact(artifact_id="a", parts=[new_text_part("abcdef")])])

    async def test_result_pages_are_bounded_and_reconstruct_exact_text(self):
        client = BridgeClient()
        with patch.object(client, "_get_task", new=AsyncMock(return_value=self.task)):
            first = await client.task("url", "task").result_page(limit=3)
            second = await client.task("url", "task").result_page(cursor=first["next_cursor"], limit=3)
        self.assertEqual(first["text"] + second["text"], "abcdef")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["state"], "TASK_STATE_COMPLETED")

    async def test_transcript_includes_prompt_and_artifact_with_stable_pagination(self):
        client = BridgeClient()
        with patch.object(client, "_get_task", new=AsyncMock(return_value=self.task)):
            first = await client.task("url", "task").transcript(limit=1)
            second = await client.task("url", "task").transcript(cursor=first["next_cursor"], limit=1)
        self.assertEqual(first["items"][0]["message"]["parts"][0]["text"], "prompt")
        self.assertEqual(second["items"][0]["artifact"]["artifactId"], "a")
        self.assertIsNone(second["next_cursor"])

    async def test_page_arguments_and_nonfinite_wait_budgets_are_rejected(self):
        handle = BridgeClient().task("url", "task")
        for kwargs in ({"limit": 0}, {"limit": 60001}, {"cursor": -1}, {"cursor": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await handle.result_page(**kwargs)
        for timeout in (float("nan"), float("inf"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await handle.wait(timeout)

    def test_structured_error_is_available_without_parsing_display_text(self):
        self.task.status.state = TaskState.TASK_STATE_FAILED
        self.task.metadata["agent_bridge.error"] = {"code": "worker_timeout", "type": "TimeoutError", "retryable": False}
        result = _task_result("url", self.task)
        self.assertEqual(result.error["code"], "worker_timeout")

    async def test_large_transcript_event_is_chunked_without_loss_and_pages_stay_bounded(self):
        self.task.history[0].parts[0].text = "x" * 150000
        client = BridgeClient()
        collected, cursor = [], 0
        with patch.object(client, "_get_task", new=AsyncMock(return_value=self.task)):
            while cursor is not None:
                page = await client.task("url", "task").transcript(cursor=cursor)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 61000)
                collected.extend(page["items"])
                cursor = page["next_cursor"]
        encoded = "".join(item["text"] for item in collected if item["kind"] == "json_chunk")
        self.assertEqual(json.loads(encoded)["message"]["parts"][0]["text"], "x" * 150000)
