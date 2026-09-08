"""Conservative two-way file/Memo reconciliation with target-scoped state."""
import hashlib
import json
import os
from pathlib import Path
import uuid
import memo_protocol as mp
import ic35_protocol as p

MODES = ("Aus", "Lokaler Ordner", "Cloud-Ordner (Desktop-Sync)")
MARKER = ".ic35-notes-target.json"


def acquire_run_lock(data_dir):
    """Windows process lock, automatically released on process exit."""
    import msvcrt
    path = Path(data_dir) / "full_sync.lock"
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        stream.close()
        raise RuntimeError("Ein anderer v3.1-Synchronisationslauf ist bereits aktiv.")
    return stream


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def configure(folder):
    root = Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Bitte einen vorhandenen Notizordner auswählen.")
    marker = root / MARKER
    if not marker.exists():
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"version": 1, "id": str(uuid.uuid4())}, stream)
    info = read_json(marker)
    if info.get("version") != 1 or not info.get("id"):
        raise ValueError("Ungültige Notizordner-Kennung.")
    if str(uuid.UUID(info["id"])) != info["id"]:
        raise ValueError("Ungültige Notizordner-Kennung.")
    return str(root), info["id"]


def content(fields):
    return (fields["Betreff"] or "", (fields["Notizen"] or "").replace("\r\n", "\n").replace("\r", "\n"))


def file_fields(raw):
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    title, sep, body = text.partition("\n")
    fields = {"Betreff": title, "Notizen": body, "category-id": 15, "category": "Unfiled"}
    mp.encode(fields)
    return fields


def file_bytes(fields):
    title, body = content(fields)
    if "\n" in title or "\r" in title:
        raise ValueError("Memo-Betreff enthält einen Zeilenumbruch.")
    return (title + "\n" + body).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def scan(root):
    files = {}
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in (".txt", ".md"):
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ValueError(f"Unzulässiger Notizpfad: {path.name}")
        if path.stat().st_size > 8192:
            raise ValueError(f"Notizdatei zu groß: {path.name}; IC35-Text maximal 255 Byte.")
        raw = path.read_bytes()
        try:
            fields = file_fields(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
        files[path.name] = {"fields": fields, "hash": digest(raw)}
    return files


def device_records(db):
    if not db or db.get("error") or db.get("count") != len(db.get("records", [])):
        raise ValueError("Memo-Datenbank unvollständig; keine Notizen werden geändert.")
    result = {}
    for rec in db["records"]:
        if rec.get("deleted"):
            continue
        fields = mp.values(rec)
        rid = str(rec["record_id"])
        if rid in result or not 0 < int(rid) <= 0xFFFFFF:
            raise ValueError("Ungültige oder doppelte Memo-ID.")
        mp.encode(fields)
        file_bytes(fields)
        result[rid] = fields
    return result


def prepare(config, data_dir, export):
    if config.get("mode", "Aus") == "Aus":
        return None
    if config.get("mode") not in MODES or str(uuid.UUID(config["target_id"])) != config["target_id"]:
        raise ValueError("Ungültiges Notiz-Ziel.")
    root = Path(config["folder"]).resolve(strict=True)
    marker = read_json(root / MARKER, {})
    if marker.get("id") != config.get("target_id"):
        raise ValueError("Notizordner nicht verfügbar oder Kennung fehlt. Kein Löschabgleich.")
    state_dir = Path(data_dir) / "memo_sync" / config["target_id"]
    state_path = state_dir / "state.json"
    state = read_json(state_path, {"version": 1, "bindings": {}, "device": export["device"]})
    if state.get("version") != 1 or state.get("device") != export["device"]:
        raise ValueError("Notiz-State gehört zu einem anderen IC35 oder ist inkompatibel.")
    files = scan(root)
    device = device_records(export.get("databases", {}).get("Memo"))
    if (state_dir / "pending.json").exists():
        recover_pending(state_dir, state_path, state, device, files)
    operations, conflicts = build_plan(state["bindings"], device, files, config.get("extension", ".md"))
    if conflicts:
        atomic_json(state_dir / "conflicts.json", {"conflicts": conflicts})
        raise ValueError("Notiz-Konflikt: " + "; ".join(conflicts) + ". Beide Fassungen bleiben erhalten.")
    return {"root": root, "state_dir": state_dir, "state_path": state_path,
            "state": state, "files": files, "device": device, "operations": operations,
            "target_id": config["target_id"]}


def recover_pending(state_dir, state_path, state, device, files):
    """Resolve only an exactly observable pre/post state after an interrupted operation."""
    pending_path = state_dir / "pending.json"
    pending = read_json(pending_path)
    def blocked():
        raise ValueError(f"Unvollständiger Notizlauf: {pending_path}. "
                         "Zustand nicht eindeutig; siehe README_NOTIZEN.md.")
    if not all(k in pending for k in ("operation", "device_before", "file_before")):
        blocked()
    op = pending["operation"]
    kind, rid, name = op["kind"], op["rid"], op["file"]
    before = pending["device_before"]
    right = files.get(name)
    file_unchanged = right == pending["file_before"]
    success = False
    if kind == "write_device":
        if not file_unchanged:
            blocked()
        if rid is None:
            new = {r: f for r, f in device.items() if r not in before}
            matches = [r for r, f in new.items() if f == op["fields"]]
            if len(matches) == 1 and len(new) == 1:
                rid, success = matches[0], True
            elif new or device != before:
                blocked()
        elif device.get(rid) == op["fields"]:
            success = True
        elif device.get(rid) != before.get(rid):
            blocked()
    elif kind == "delete_device":
        if not file_unchanged:
            blocked()
        success = rid not in device
        if not success and device.get(rid) != before.get(rid):
            blocked()
    elif kind == "write_file":
        if device.get(rid) != before.get(rid):
            blocked()
        success = right is not None and right["hash"] == digest(file_bytes(op["fields"]))
        if not success and not file_unchanged:
            blocked()
    elif kind == "delete_file":
        if rid in device:
            blocked()
        success = right is None
        if not success and not file_unchanged:
            blocked()
    elif kind in ("bind", "forget"):
        # No external effects. Normal reconciliation can safely redo these.
        pass
    else:
        blocked()
    if success:
        if kind in ("delete_device", "delete_file"):
            state["bindings"].pop(rid, None)
        else:
            state["bindings"][rid] = {"file": name, "fields": op["fields"]}
        atomic_json(state_path, state)
    pending_path.unlink()


def build_plan(bindings, device, files, extension=".md"):
    if extension not in (".txt", ".md"):
        raise ValueError("Notizformat muss .txt oder .md sein.")
    ops, conflicts = [], []
    used_i, used_f = set(), set()
    for rid, binding in bindings.items():
        name = binding["file"]
        if Path(name).name != name or name in used_f:
            raise ValueError("Ungültige Dateizuordnung im Notiz-State.")
        old = binding["fields"]
        left, right = device.get(rid), files.get(name)
        # Recognize a unique rename by its last common text.
        if right is None:
            candidates = [n for n, f in files.items() if n not in used_f
                          and n not in {b["file"] for b in bindings.values()}
                          and content(f["fields"]) == content(old)]
            if len(candidates) == 1:
                name, right = candidates[0], files[candidates[0]]
        used_i.add(rid)
        used_f.add(name)
        if left is None and right is None:
            ops.append({"kind": "forget", "rid": rid, "file": name})
        elif left is None:
            if content(right["fields"]) != content(old):
                conflicts.append(f"{name}: IC35 gelöscht, Datei geändert")
            else:
                ops.append({"kind": "delete_file", "rid": rid, "file": name})
        elif right is None:
            if left != old:
                conflicts.append(f"Memo {rid}: Datei gelöscht, IC35 geändert")
            else:
                ops.append({"kind": "delete_device", "rid": rid, "file": name})
        else:
            lc, rc = content(left) != content(old), content(right["fields"]) != content(old)
            if lc and rc and content(left) != content(right["fields"]):
                conflicts.append(f"{name}: beide Seiten geändert")
                continue
            if rc and content(left) != content(right["fields"]):
                merged = dict(left)
                merged.update({key: right["fields"][key] for key in ("Betreff", "Notizen")})
                ops.append({"kind": "write_device", "rid": rid, "file": name, "fields": merged})
            else:
                ops.append({"kind": "write_file" if lc and not rc else "bind",
                            "rid": rid, "file": name, "fields": left})
    # Initial union: pair equal content one-to-one; never deduplicate same-side notes.
    for rid, fields in device.items():
        if rid in used_i:
            continue
        matches = [name for name, f in files.items() if name not in used_f
                   and content(fields) == content(f["fields"])]
        if matches:
            name = matches[0]
            ops.append({"kind": "bind", "rid": rid, "file": name, "fields": fields})
        else:
            name = f"IC35-Memo-{rid}{extension}"
            while name in files or name in used_f:
                name = f"IC35-Memo-{rid}-{uuid.uuid4().hex[:8]}{extension}"
            ops.append({"kind": "write_file", "rid": rid, "file": name, "fields": fields})
        used_f.add(name)
    for name, entry in files.items():
        if name not in used_f:
            ops.append({"kind": "write_device", "rid": None, "file": name, "fields": entry["fields"]})
    for op in ops:
        if "fields" in op:
            mp.encode(op["fields"])
    return ops, conflicts


def apply(plan, ser, export, logger=print):
    if plan is None:
        return
    root, state, state_dir = plan["root"], plan["state"], plan["state_dir"]
    if read_json(root / MARKER, {}).get("id") != plan["target_id"] or scan(root) != plan["files"]:
        raise RuntimeError("Notizordner wurde seit der Planung verändert.")
    fd = p.open_database(ser, "Memo")
    if fd is None:
        raise RuntimeError("Memo-Datenbank nicht zu öffnen.")
    try:
        records = mp.read_all(ser, fd)
        if device_records({"count": len(records), "records": records}) != plan["device"]:
            raise RuntimeError("IC35-Memos wurden seit der Planung verändert.")
        backup = state_dir / "backups" / uuid.uuid4().hex
        atomic_json(backup / "before.json", {"state": state, "device": plan["device"], "plan": plan["operations"]})
        for name in plan["files"]:
            (backup / name).write_bytes((root / name).read_bytes())
        expected_files = dict(plan["files"])
        current_device = dict(plan["device"])
        for op in plan["operations"]:
            kind, rid, name = op["kind"], op["rid"], op["file"]
            path = root / name
            if scan(root) != expected_files:
                raise RuntimeError("Notizdateien parallel geändert. Abgleich angehalten.")
            # Persist intent BEFORE the first side effect. A crash cannot silently replay CREATE.
            atomic_json(state_dir / "pending.json", {"operation": op, "backup": str(backup),
                        "device_before": current_device, "file_before": expected_files.get(name)})
            if kind == "write_device":
                rec = mp.write(ser, fd, op["fields"], int(rid) if rid else None)
                rid = str(rec["record_id"])
                op["fields"] = mp.values(rec)
                current_device[rid] = op["fields"]
            elif kind == "delete_device":
                mp.delete(ser, fd, int(rid))
                current_device.pop(rid, None)
            elif kind == "write_file":
                raw = file_bytes(op["fields"])
                if name not in expected_files:
                    with path.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                else:
                    tmp = root / (".ic35-write-" + uuid.uuid4().hex)
                    try:
                        with tmp.open("xb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                        if digest(path.read_bytes()) != expected_files[name]["hash"]:
                            raise RuntimeError("Notizdatei unmittelbar vor Schreiben verändert.")
                        os.replace(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                expected_files[name] = {"fields": file_fields(raw), "hash": digest(raw)}
            elif kind == "delete_file":
                if digest(path.read_bytes()) != expected_files[name]["hash"]:
                    raise RuntimeError("Notizdatei vor Löschung verändert.")
                path.unlink()
                del expected_files[name]
            if kind in ("delete_device", "delete_file", "forget"):
                state["bindings"].pop(rid, None)
            else:
                state["bindings"][rid] = {"file": name, "fields": op["fields"]}
            atomic_json(plan["state_path"], state)
            (state_dir / "pending.json").unlink()
            logger(f"Notizen: {kind} · {name} · IC35 {rid}")
        final = mp.read_all(ser, fd)
        export["databases"]["Memo"] = {"count": len(final), "records": final}
    finally:
        p.close_database(ser, "Memo", fd)
