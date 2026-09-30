import asyncio
import unittest

from agent_bridge.a2a_server import SessionManager


class Session:
    def __init__(self):
        self.closed = False
        self.active = 0
        self.max_active = 0
        self.turns = 0

    async def ask(self, prompt):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        self.turns += 1
        return f"{self.turns}:{prompt}"

    async def close(self):
        self.closed = True


class Backend:
    def __init__(self):
        self.sessions = []

    async def open_session(self, model=None, *, reasoning_effort=None, read_only=False,
                           tool_policy=None):
        session = Session()
        self.sessions.append(session)
        return session


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_session_serializes_turns_and_pins_policy(self):
        backend = Backend()
        manager = SessionManager(backend)
        replies = await asyncio.gather(*(
            manager.run("s", f"turn {n}", "m", None, False, "no_tools")
            for n in range(5)
        ))
        self.assertEqual(len(replies), 5)
        self.assertEqual(len(backend.sessions), 1)
        self.assertEqual(backend.sessions[0].max_active, 1)
        with self.assertRaisesRegex(ValueError, "cannot change"):
            await manager.run("s", "new", "m", None, False, "read_only")
        self.assertTrue(await manager.close("s"))
        self.assertFalse(await manager.close("s"))
        self.assertTrue(backend.sessions[0].closed)

    async def test_idle_reaping_and_close_all(self):
        backend = Backend()
        manager = SessionManager(backend, idle_seconds=0)
        await manager.run("a", "one", None, None, False)
        await manager.run("b", "two", None, None, False)
        for record in manager.sessions.values():
            record.last_used -= 1
        await manager.reap_idle()
        self.assertEqual(manager.sessions, {})
        self.assertTrue(all(session.closed for session in backend.sessions))
        await manager.run("c", "three", None, None, False)
        await manager.close_all()
        self.assertEqual(manager.sessions, {})


if __name__ == "__main__":
    unittest.main()
