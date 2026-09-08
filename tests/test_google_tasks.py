import copy
import sys
import tempfile
import types
from pathlib import Path
import unittest
from unittest.mock import patch
sys.modules.setdefault("serial", types.ModuleType("serial"))
import google_tasks_bridge as g
import todo_protocol as tp


def fields(title="Aufgabe", done=0):
    return {"Start": "20260901", "Ende": "20260906", "Erledigt": done,
            "Prioritaet": 2, "Betreff": title, "Notizen": "Text", "category-id": 16, "category": "Business"}


def task(tid="a", title="Aufgabe", done=0):
    return dict(g.body(g.semantic(fields(title, done))), id=tid, etag='"1"')


def record(rid, f):
    return {"record_id": int(rid), "file_id": 7, "fields": {k: {"value": v} for k, v in f.items()}}


class Request:
    def __init__(self, action):
        self.action, self.headers = action, {}
    def execute(self, **kwargs):
        return copy.deepcopy(self.action(self.headers))


class Google:
    def __init__(self, tasks=None):
        self.data = tasks or {}
        self.calls = []
        self.lost_response = False
    def tasklists(self):
        return types.SimpleNamespace(get=lambda **k: Request(lambda h: {"id": k['tasklist']}))
    def tasks(self):
        return self
    def list(self, **kw):
        self.calls.append(("list", kw))
        index = int(kw.get("pageToken") or 0)
        items = list(self.data.values())
        result = {"items": items[index:index + 2]}
        if index + 2 < len(items):
            result["nextPageToken"] = str(index + 2)
        return Request(lambda h: result)
    def get(self, task, **kw):
        return Request(lambda h: self.data[task])
    def insert(self, body, **kw):
        def action(headers):
            tid = "new" + str(len(self.data))
            self.data[tid] = dict(body, id=tid, etag='"1"')
            self.calls.append(("insert", body))
            if self.lost_response:
                raise TimeoutError("Response lost after successful insert")
            return self.data[tid]
        return Request(action)
    def patch(self, task, body, **kw):
        def action(headers):
            assert headers["If-Match"] == self.data[task]["etag"]
            self.data[task].update(body)
            self.data[task]["etag"] = '"2"'
            self.calls.append(("patch", body))
            return self.data[task]
        return Request(action)
    def delete(self, task, **kw):
        def action(headers):
            assert headers["If-Match"] == self.data[task]["etag"]
            self.data[task]["deleted"] = True
            return {}
        return Request(action)


class Tests(unittest.TestCase):
    def test_hardware_start_date_normalization_and_recovery(self):
        expected = g.to_fields(g.google_semantic(task()))
        actual = dict(expected, Start=expected['Ende'])
        self.assertTrue(tp.matches_written(actual, expected))
        self.assertFalse(tp.matches_written(dict(actual, Betreff='Andere Aufgabe'), expected))
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            backup = directory / 'backups' / 'before.json'
            g.atomic_json(backup, {'device': {}})
            g.atomic_json(directory / 'pending.json', {'operation': {
                'kind': 'write_device', 'rid': None, 'task_id': 'a', 'fields': expected}, 'backup': str(backup)})
            state = {'bindings': {}}
            g.recover_device_create(directory, state, {'4': actual, '5': actual}, {'a': task()})
            self.assertTrue((directory / 'pending.json').exists())
            g.recover_device_create(directory, state, {'4': actual}, {'a': task()})
            self.assertFalse((directory / 'pending.json').exists())
            self.assertEqual(state['bindings']['4']['fields'], actual)
            self.assertEqual(g.build_plan(state['bindings'], {'4': actual}, {'a': task()})[0][0]['kind'], 'bind')

    def setUp(self):
        self.f = fields()
        self.bindings = {"1": {"task_id": "a", "fields": self.f, "semantic": g.semantic(self.f)}}

    def test_mapping_preserves_local_fields_and_completion(self):
        result = g.to_fields(g.google_semantic(task(done=1)), self.f)
        for key in ("Start", "Prioritaet", "category-id", "category"):
            self.assertEqual(result[key], self.f[key])
        self.assertEqual(result["Erledigt"], 1)
        self.assertEqual(g.to_fields(g.google_semantic(task()))["category-id"], 18)
        self.assertEqual(g.body(dict(g.semantic(self.f), due=""))["due"], None)

    def test_initial_union_and_completion_pairing(self):
        ops, conflicts, skipped = g.build_plan({}, {"1": self.f}, {"a": task()})
        self.assertEqual([o["kind"] for o in ops], ["bind"])
        self.assertFalse(conflicts or skipped)
        self.assertEqual(g.build_plan({}, {}, {"a": task(done=1)})[0][0]["fields"]["Erledigt"], 1)

    def test_update_both_ways_and_reopen(self):
        for left, right, expected in ((fields("Lokal"), task(), "write_google"),
                                     (self.f, task(done=1), "write_device")):
            ops, conflicts, _ = g.build_plan(self.bindings, {"1": left}, {"a": right})
            self.assertFalse(conflicts)
            self.assertEqual(ops[0]["kind"], expected)
        b = {"1": {"task_id": "a", "fields": fields(done=1), "semantic": g.semantic(fields(done=1))}}
        self.assertEqual(g.build_plan(b, {"1": fields(done=1)}, {"a": task()})[0][0]["fields"]["Erledigt"], 0)

    def test_deletes_and_conflicts(self):
        self.assertEqual(g.build_plan(self.bindings, {}, {"a": task()})[0][0]["kind"], "delete_google")
        self.assertEqual(g.build_plan(self.bindings, {"1": self.f}, {})[0][0]["kind"], "delete_device")
        self.assertTrue(g.build_plan(self.bindings, {"1": fields("Lokal")}, {"a": task(title="Cloud")})[1])
        self.assertTrue(g.build_plan(self.bindings, {}, {"a": task(done=1)})[1])
        self.assertTrue(g.build_plan(self.bindings, {"1": fields("Lokal")}, {})[1])

    def test_hierarchy_and_assigned_excluded(self):
        remote = {"a": task(), "b": dict(task("b"), parent="a"),
                  "c": dict(task("c"), assignmentInfo={"contextType": "DOCUMENT"})}
        ops, _, skipped = g.build_plan({}, {}, remote)
        self.assertFalse(ops)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(g.build_plan(self.bindings, {"1": self.f}, remote)[1])

    def test_full_pagination_includes_completed_hidden_deleted(self):
        svc = Google({str(i): task(str(i)) for i in range(5)})
        self.assertEqual(len(g.snapshot(svc, "list")), 5)
        self.assertEqual(len(svc.calls), 3)
        self.assertTrue(all(kw['showHidden'] and kw['showCompleted'] and kw['showDeleted']
                            for _, kw in svc.calls))

    def test_limits_and_bad_snapshot(self):
        for f in (fields("x" * 61), fields("😀")):
            with self.assertRaises(ValueError):
                tp.encode(f)
        with self.assertRaises(ValueError):
            g.device_snapshot({"count": 1, "records": []})

    def test_etag_guard(self):
        with self.assertRaises(RuntimeError):
            g.checked_task(Google({"a": task()}), "list", "a", {"etag": "changed"})

    def test_wire_format(self):
        packets = []
        f = dict(self.f, Notizen="x" * 255)
        raw = bytes.fromhex("09 00 00 07 00") + tp.encode(f)
        with patch.object(tp.p, "make_fragment_command", side_effect=lambda a, b, c: packets.append((a,b,c)) or c), \
             patch.object(tp.p, "l1_transaction", return_value=b"ok"), \
             patch.object(tp.p, "_decode_write_response", return_value=(9, 7)), \
             patch.object(tp.p, "read_record_raw_by_id", return_value=raw):
            tp.write(None, b"\x04\x00", f)
            self.assertEqual(packets[0][2][:13], bytes.fromhex("01 08 04 00 00 00 00 00 10 00 00 00 00"))
            self.assertEqual(packets[-1][:2], (0x82, 0x49))
            self.assertEqual(tp.values(tp.p.decode_record("To Do List", raw, 0)), f)

    def harness(self, td, dev, svc):
        config = {"enabled": True, "list_id": "list"}
        export = {"device": "test", "databases": {}}
        def write(ser, fd, f, rid=None):
            rid = str(rid or max(map(int, dev), default=0) + 1)
            dev[rid] = copy.deepcopy(f)
            return record(rid, f)
        def run():
            records = [record(r,f) for r,f in dev.items()]
            export["databases"]["To Do List"] = {"count": len(records), "records": records}
            plan = g.prepare(config, td, export, svc, g.snapshot(svc, "list"))
            with patch.object(tp.p, "open_database", return_value=b"fd"), \
                 patch.object(tp.p, "close_database"), \
                 patch.object(tp, "read_all", side_effect=lambda *_: [record(r,f) for r,f in dev.items()]), \
                 patch.object(tp, "write", side_effect=write), \
                 patch.object(tp, "delete", side_effect=lambda s, fd, rid: dev.pop(str(rid))), \
                 patch.object(tp.p, "read_record_raw_by_id", side_effect=lambda s,n,fd,rid: str(rid)), \
                 patch.object(tp.p, "decode_record", side_effect=lambda n,r,i: record(r, dev[r])):
                return g.apply(plan, None, export, lambda _: None)
        return run

    def test_end_to_end_crud(self):
        with tempfile.TemporaryDirectory() as td:
            dev, svc = {"1": self.f}, Google()
            run = self.harness(td, dev, svc)
            self.assertEqual(run()["write_google"], 1)
            tid = next(iter(svc.data))
            self.assertEqual(run()["write_google"], 0)
            dev['1']['Betreff'] = 'Vom IC35'
            self.assertEqual(run()["write_google"], 1)
            self.assertEqual(svc.data[tid]['title'], 'Vom IC35')
            svc.data[tid]['status'] = 'completed'
            self.assertEqual(run()["write_device"], 1)
            self.assertEqual(dev['1']['Erledigt'], 1)
            svc.data['fresh'] = task('fresh', 'Neu')
            self.assertEqual(run()["write_device"], 1)
            self.assertEqual(len(dev), 2)
            svc.data[tid]['deleted'] = True
            self.assertEqual(run()["delete_device"], 1)
            dev.clear()
            self.assertEqual(run()["delete_google"], 1)
            self.assertTrue(svc.data['fresh']['deleted'])

    def test_uncertain_insert_never_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            svc = Google()
            svc.lost_response = True
            run = self.harness(td, {"1": self.f}, svc)
            with self.assertRaises(TimeoutError):
                run()
            self.assertEqual(len(svc.data), 1)
            with self.assertRaisesRegex(RuntimeError, "Unbestätigte"):
                run()
            self.assertEqual(len(svc.data), 1)
            self.assertEqual(len(list(Path(td).rglob('pending.json'))), 1)


if __name__ == '__main__':
    unittest.main()
