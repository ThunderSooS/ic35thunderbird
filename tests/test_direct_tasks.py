import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
import sys
import types
sys.modules.setdefault("serial", types.ModuleType("serial"))
import direct_tasks_sync as direct
import google_tasks_bridge as tasks
import manager_protocol as manager
from test_google_tasks import Google, task, record


class DirectTests(unittest.TestCase):
    def test_google_to_empty_device_without_calendar_or_radicale(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            google = Google({"a": task(), "b": task("b", "Zweite Aufgabe")})
            device = {}
            def write(ser, fd, fields, rid=None):
                rid = str(rid or len(device) + 1)
                device[rid] = fields
                return record(rid, fields)
            stack.enter_context(patch.object(direct, "acquire_run_lock", return_value=Mock()))
            stack.enter_context(patch.object(tasks, "service", return_value=google))
            for name in ("manager_connect", "manager_disconnect"):
                stack.enter_context(patch.object(manager, name))
            backup = stack.enter_context(patch.object(manager, "backup_database", side_effect=AssertionError('Unexpected full backup')))
            for name in ("do_welcome_and_reopen", "power_request", "disconnect"):
                stack.enter_context(patch.object(direct.proto, name, return_value=True))
            stack.enter_context(patch.object(direct.proto, "identify", return_value="IC35-test"))
            stack.enter_context(patch.object(direct.proto, "authenticate_once", return_value=b"\x01\x01"))
            stack.enter_context(patch.object(direct.proto, "read_sync_info"))
            stack.enter_context(patch.object(direct.proto, "open_database", return_value=b"fd"))
            stack.enter_context(patch.object(direct.proto, "close_database"))
            stack.enter_context(patch.object(direct.todo, "read_all", side_effect=lambda *_: [record(r,f) for r,f in device.items()]))
            stack.enter_context(patch.object(direct.todo, "write", side_effect=write))
            notify, open_serial = Mock(), Mock(return_value=Mock())
            result = direct.run("COM5", {"list_id": "list", "enabled": False}, td,
                                "nonexistent-calendar-credentials.json", open_serial, notify, lambda _: None)
            self.assertEqual(result['count'], 2)
            self.assertEqual(result['stats']['write_device'], 2)
            self.assertEqual(open_serial.call_count, 1)
            backup.assert_not_called()
            self.assertEqual([c.args[1] for c in notify.call_args_list if c.args[0] == 'sound'], ['start'])


if __name__ == '__main__':
    unittest.main()
