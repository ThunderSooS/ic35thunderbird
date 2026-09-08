import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Pure protocol/sync tests require no serial hardware or installed driver.
sys.modules.setdefault("serial", types.ModuleType("serial"))
import memo_protocol as mp
import memo_sync as m


def fields(title="Titel", body="Text"):
    return {"Betreff": title, "Notizen": body, "category-id": 15, "category": "Unfiled"}


def record(rid, data):
    return {"record_id": int(rid), "file_id": 6,
            "fields": {k: {"value": v} for k, v in data.items()}}


class MemoTests(unittest.TestCase):
    def setUp(self):
        self.a = fields()
        self.b = fields(body="Neu")
        self.binding = {"1": {"file": "a.md", "fields": self.a}}

    def plan(self, dev, files, bindings=None):
        return m.build_plan(self.binding if bindings is None else bindings,
                            dev, {n: {"fields": f} for n, f in files.items()})

    def test_initial_union_pairs_without_duplicates(self):
        ops, conflicts = self.plan({"1": self.a, "2": self.b}, {"a.txt": self.a}, {})
        self.assertFalse(conflicts)
        self.assertEqual([o["kind"] for o in ops], ["bind", "write_file"])

    def test_both_create_directions(self):
        self.assertEqual(self.plan({}, {"a.md": self.a}, {})[0][0]["kind"], "write_device")
        self.assertEqual(self.plan({"1": self.a}, {}, {})[0][0]["kind"], "write_file")

    def test_update_directions_and_category_preservation(self):
        a = dict(self.a, **{"category-id": 13, "category": "Business"})
        op = self.plan({"1": a}, {"a.md": self.b})[0][0]
        self.assertEqual(op["kind"], "write_device")
        self.assertEqual(op["fields"]["category-id"], 13)
        self.assertEqual(self.plan({"1": self.b}, {"a.md": self.a})[0][0]["kind"], "write_file")

    def test_delete_directions(self):
        self.assertEqual(self.plan({}, {"a.md": self.a})[0][0]["kind"], "delete_file")
        self.assertEqual(self.plan({"1": self.a}, {})[0][0]["kind"], "delete_device")
        self.assertEqual(self.plan({}, {})[0][0]["kind"], "forget")

    def test_conflicts(self):
        self.assertTrue(self.plan({"1": self.b}, {"a.md": fields(body="anders")})[1])
        self.assertTrue(self.plan({}, {"a.md": self.b})[1])
        self.assertTrue(self.plan({"1": self.b}, {})[1])

    def test_converged_and_rename(self):
        self.assertEqual(self.plan({"1": self.b}, {"a.md": self.b})[0][0]["kind"], "bind")
        op = self.plan({"1": self.a}, {"umbenannt.txt": self.a})[0][0]
        self.assertEqual((op["kind"], op["file"]), ("bind", "umbenannt.txt"))

    def test_strict_limits_and_encoding(self):
        mp.encode(fields("ä" * 60, "ü" * 255))
        for f in (fields("a" * 61), fields(body="a" * 256), fields(body="😀"), fields(body="\x00")):
            with self.assertRaises(ValueError):
                mp.encode(f)

    def test_file_roundtrip(self):
        f = fields("Überschrift", "Zeile 1\nZeile 2\n")
        self.assertEqual(m.file_fields(m.file_bytes(f)), f)
        self.assertEqual(m.file_fields(b"Titel\r\nText"), self.a)

    def test_incomplete_snapshot(self):
        for db in ({"error": "read", "records": []}, {"count": 1, "records": []},
                   {"count": 1, "records": [{"record_id": 1, "file_id": 6, "fields": {}}]}):
            with self.assertRaises(ValueError):
                m.device_records(db)

    def test_wire_create_update_fragmentation(self):
        f = fields(body="a" * 255)
        raw = b"\x07\x00\x00\x06\x00" + mp.encode(f)
        packets = []
        def fragment(l2, l3, data):
            packets.append((l2, l3, data))
            return data
        with patch.object(mp.p, "make_fragment_command", side_effect=fragment), \
             patch.object(mp.p, "l1_transaction", return_value=b"ok"), \
             patch.object(mp.p, "_decode_write_response", return_value=(7, 6)), \
             patch.object(mp.p, "read_record_raw_by_id", return_value=raw):
            self.assertEqual(mp.write(None, b"\x03\x00", f)["record_id"], 7)
            self.assertEqual(packets[0][2][:13], bytes.fromhex("01 08 03 00 00 00 00 00 60 16 99 00 00"))
            self.assertEqual(packets[-1][:2], (0x82, 0x49))
            self.assertEqual(b"".join([packets[0][2][17:]] + [x[2] for x in packets[1:]]), mp.encode(f))
            packets.clear()
            mp.write(None, b"\x03\x00", f, 7)
            self.assertEqual(packets[0][2][:8], bytes.fromhex("01 09 03 00 07 00 00 06"))

    def test_end_to_end_and_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "notes"
            root.mkdir()
            folder, target = m.configure(root)
            cfg = {"mode": m.MODES[1], "folder": folder, "target_id": target}
            dev = {"1": self.a}
            export = {"device": "test", "databases": {}}
            def update_export():
                records = [record(rid, f) for rid, f in dev.items()]
                export["databases"]["Memo"] = {"count": len(records), "records": records}
            def write(ser, fd, f, rid=None):
                rid = str(rid or max(map(int, dev), default=0) + 1)
                dev[rid] = copy.deepcopy(f)
                return record(rid, f)
            def run():
                update_export()
                plan = m.prepare(cfg, Path(td) / "data", export)
                m.apply(plan, None, export, lambda _: None)
                return plan
            with patch.object(m.p, "open_database", return_value=b"fd"), \
                 patch.object(m.p, "close_database"), \
                 patch.object(mp, "read_all", side_effect=lambda *_: [record(r, f) for r, f in dev.items()]), \
                 patch.object(mp, "write", side_effect=write) as writer, \
                 patch.object(mp, "delete", side_effect=lambda s, fd, rid: dev.pop(str(rid))):
                run()
                file = root / "IC35-Memo-1.md"
                self.assertEqual(m.file_fields(file.read_bytes()), self.a)
                run()
                writer.assert_not_called()
                file.write_bytes(m.file_bytes(self.b))
                run()
                self.assertEqual(dev["1"], self.b)
                dev["1"] = fields(body="Vom IC35")
                run()
                self.assertEqual(m.file_fields(file.read_bytes()), dev["1"])
                (root / "neu.txt").write_bytes(m.file_bytes(self.a))
                run()
                self.assertEqual(len(dev), 2)
                file.unlink()
                run()
                self.assertNotIn("1", dev)
                dev.clear()
                run()
                self.assertFalse((root / "neu.txt").exists())

    def test_missing_target_and_pending_block(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder, target = m.configure(root)
            cfg = {"mode": m.MODES[1], "folder": folder, "target_id": target}
            export = {"device": "test", "databases": {"Memo": {"count": 0, "records": []}}}
            state_dir = root / "memo_sync" / target
            m.atomic_json(state_dir / "pending.json", {})
            with self.assertRaisesRegex(ValueError, "Unvollständiger"):
                m.prepare(cfg, root, export)

    def test_crash_recovery_create_no_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            op = {"kind": "write_device", "rid": None, "file": "a.md", "fields": self.a}
            file = {"fields": self.a, "hash": "test"}
            pending = {"operation": op, "device_before": {}, "file_before": file}
            m.atomic_json(root / "pending.json", pending)
            state = {"bindings": {}}
            m.recover_pending(root, root / "state.json", state, {"7": self.a}, {"a.md": file})
            self.assertEqual(state["bindings"]["7"]["file"], "a.md")
            self.assertFalse((root / "pending.json").exists())
            m.atomic_json(root / "pending.json", pending)
            with self.assertRaisesRegex(ValueError, "nicht eindeutig"):
                m.recover_pending(root, root / "state.json", state,
                                  {"7": self.a, "8": self.a}, {"a.md": file})
            self.assertTrue((root / "pending.json").exists())

    def test_crash_recovery_unapplied_operation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            op = {"kind": "write_device", "rid": "1", "file": "a.md", "fields": self.b}
            file = {"fields": self.b, "hash": "test"}
            m.atomic_json(root / "pending.json", {"operation": op,
                "device_before": {"1": self.a}, "file_before": file})
            state = {"bindings": copy.deepcopy(self.binding)}
            m.recover_pending(root, root / "state.json", state, {"1": self.a}, {"a.md": file})
            self.assertEqual(state["bindings"], self.binding)
            self.assertFalse((root / "pending.json").exists())

    def test_concurrent_file_change_stops_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder, target = m.configure(root)
            cfg = {"mode": m.MODES[1], "folder": folder, "target_id": target}
            export = {"device": "test", "databases": {"Memo": {"count": 0, "records": []}}}
            plan = m.prepare(cfg, root / "data", export)
            (root / "new.md").write_bytes(m.file_bytes(self.a))
            with patch.object(m.p, "open_database") as opener:
                with self.assertRaisesRegex(RuntimeError, "verändert"):
                    m.apply(plan, None, export)
                opener.assert_not_called()
            (root / m.MARKER).unlink()
            with self.assertRaisesRegex(ValueError, "Kennung"):
                m.prepare(cfg, root, export)


if __name__ == "__main__":
    unittest.main()
