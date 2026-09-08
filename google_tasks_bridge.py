"""Google Tasks <-> IC35 To Do List, independent of calendar and memo state."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid
import todo_protocol as tp
from memo_sync import atomic_json, read_json

SCOPES = ["https://www.googleapis.com/auth/tasks"]


def service(credentials_file, token_file, interactive=False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    token_file = Path(token_file)
    creds = Credentials.from_authorized_user_file(str(token_file)) if token_file.exists() else None
    if creds and not creds.has_scopes(SCOPES):
        creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("Google Tasks bitte unter 'Aufgaben-Ziel' verbinden.")
        if not Path(credentials_file).exists():
            raise RuntimeError("Google OAuth-Datei fehlt. Zuerst 'Google verbinden' verwenden.")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                     access_type="offline", timeout_seconds=180)
        if not creds.has_scopes(SCOPES) or (creds.granted_scopes is not None
                                          and not set(SCOPES).issubset(creds.granted_scopes)):
            raise RuntimeError("Google-Tasks-Berechtigung wurde nicht erteilt.")
    atomic_json(token_file, json.loads(creds.to_json()))
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def paged(resource, **kwargs):
    items, seen = [], set()
    token = None
    while True:
        response = resource.list(maxResults=100, pageToken=token, **kwargs).execute()
        items.extend(response.get("items", []))
        token = response.get("nextPageToken")
        if not token:
            return items
        if token in seen:
            raise RuntimeError("Google Tasks: wiederholte Seitennummer; Abgleich gestoppt.")
        seen.add(token)


def tasklists(svc):
    return paged(svc.tasklists())


def snapshot(svc, list_id):
    # A missing/inaccessible list is an error, never an empty list/deletion signal.
    meta = svc.tasklists().get(tasklist=list_id).execute()
    if meta.get("id") != list_id:
        raise RuntimeError("Google-Aufgabenliste ist nicht mehr verfügbar.")
    items = paged(svc.tasks(), tasklist=list_id, showCompleted=True,
                  showHidden=True, showDeleted=True, showAssigned=True)
    result = {}
    for task in items:
        tid = task.get("id")
        if not tid or tid in result:
            raise RuntimeError("Google Tasks: unvollständiger oder doppelter Datensatz.")
        result[tid] = task
    return result


def date8(value):
    if not value:
        return ""
    datetime.strptime(value, "%Y%m%d")
    if len(value) != 8:
        raise ValueError("Ungültiges IC35-Aufgabendatum.")
    return value


def semantic(fields):
    if fields["Erledigt"] not in (0, 1):
        raise ValueError("Unbekannter Erledigt-Status auf IC35.")
    return {"title": fields["Betreff"],
            "notes": fields["Notizen"].replace("\r\n", "\n").replace("\r", "\n"),
            "due": date8(fields["Ende"]),
            "status": "completed" if fields["Erledigt"] == 1 else "needsAction"}


def google_semantic(task):
    due = task.get("due") or ""
    due = datetime.strptime(due[:10], "%Y-%m-%d").strftime("%Y%m%d") if due else ""
    status = task.get("status", "needsAction")
    if status not in ("needsAction", "completed"):
        raise ValueError("Unbekannter Google-Tasks-Status.")
    return {"title": task.get("title", ""),
            "notes": (task.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n"),
            "due": due, "status": status}


def to_fields(sem, existing=None):
    fields = dict(existing) if existing else {
        "Start": "", "Prioritaet": 1, "category-id": 18, "category": "Unfiled"}
    fields.update({"Betreff": sem["title"], "Notizen": sem["notes"],
                   "Ende": sem["due"], "Erledigt": int(sem["status"] == "completed")})
    tp.encode(fields)
    return fields


def body(sem):
    due = sem["due"]
    return {"title": sem["title"], "notes": sem["notes"], "status": sem["status"],
            "due": f"{due[:4]}-{due[4:6]}-{due[6:]}T00:00:00.000Z" if due else None}


def device_snapshot(db):
    if not db or db.get("error") or db.get("count") != len(db.get("records", [])):
        raise ValueError("Zu Erledigen nicht vollständig gelesen; Abgleich gestoppt.")
    result = {}
    for rec in db["records"]:
        if rec.get("deleted"):
            continue
        fields = tp.values(rec)
        tp.encode(fields)
        semantic(fields)
        rid = str(rec["record_id"])
        if rid in result or not 0 < int(rid) <= 0xFFFFFF:
            raise ValueError("Ungültige Aufgaben-ID auf IC35.")
        result[rid] = fields
    return result


def build_plan(bindings, device, remote):
    active = {k: t for k, t in remote.items() if not t.get("deleted")}
    # Do not flatten hierarchies or delete a parent and thereby affect its children.
    excluded = {k for k, t in active.items() if t.get("parent") or t.get("assignmentInfo")}
    excluded.update(t["parent"] for t in active.values() if t.get("parent"))
    ops, conflicts = [], []
    used_i, used_g = set(), set()
    for rid, binding in bindings.items():
        tid = binding["task_id"]
        if tid in used_g:
            raise ValueError("Doppelte Google-Aufgabenzuordnung im State.")
        used_i.add(rid)
        used_g.add(tid)
        if tid in excluded:
            conflicts.append(f"Aufgabe {tid} ist jetzt zugewiesen oder Teil einer Hierarchie")
            continue
        left, right = device.get(rid), active.get(tid)
        old = binding["semantic"]
        kind, fields = "bind", left
        if left is None and right is None:
            kind = "forget"
        elif left is None:
            if google_semantic(right) != old:
                conflicts.append(f"{right.get('title')}: IC35 gelöscht, Google geändert")
                continue
            kind = "delete_google"
        elif right is None:
            if left != binding["fields"]:
                conflicts.append(f"{left['Betreff']}: Google gelöscht, IC35 geändert")
                continue
            kind = "delete_device"
        else:
            ls, rs = semantic(left), google_semantic(right)
            if ls != old and rs != old and ls != rs:
                conflicts.append(f"{left['Betreff']}: beide Seiten geändert")
                continue
            if ls != rs:
                if rs != old:
                    kind, fields = "write_device", to_fields(rs, left)
                else:
                    kind = "write_google"
        ops.append({"kind": kind, "rid": rid, "task_id": tid, "fields": fields})
    for rid, fields in device.items():
        if rid in used_i:
            continue
        matches = [tid for tid, task in active.items() if tid not in used_g and tid not in excluded
                   and google_semantic(task) == semantic(fields)]
        tid = matches[0] if matches else None
        if tid:
            used_g.add(tid)
        ops.append({"kind": "bind" if tid else "write_google", "rid": rid,
                    "task_id": tid, "fields": fields})
    for tid, task in active.items():
        if tid not in used_g and tid not in excluded:
            ops.append({"kind": "write_device", "rid": None, "task_id": tid,
                        "fields": to_fields(google_semantic(task))})
    for op in ops:
        if op["fields"]:
            tp.encode(op["fields"])
    return ops, conflicts, [{"id": tid, "title": active[tid].get("title", ""),
                            "reason": "Unteraufgabe, übergeordnete oder zugewiesene Aufgabe"}
                           for tid in sorted(excluded) if tid in active]


def prepare(config, data_dir, export, svc, remote):
    if not config.get("enabled"):
        return None
    list_id = config["list_id"]
    directory = Path(data_dir) / "tasks_sync" / hashlib.sha256(list_id.encode()).hexdigest()
    state = read_json(directory / "state.json", {"version": 1, "list_id": list_id,
                      "device": export["device"], "bindings": {}})
    if state.get("version") != 1 or state.get("list_id") != list_id or state.get("device") != export["device"]:
        raise ValueError("Aufgaben-State passt nicht zu Gerät/Aufgabenliste.")
    device = device_snapshot(export.get("databases", {}).get("To Do List"))
    if (directory / "pending.json").exists():
        recover_device_create(directory, state, device, remote)
    if (directory / "pending.json").exists():
        raise RuntimeError(f"Unbestätigte Aufgabenoperation: {directory / 'pending.json'}. "
                           "Nicht erneut ausführen; Wiederherstellung siehe README_AUFGABEN.md.")
    ops, conflicts, skipped = build_plan(state["bindings"], device, remote)
    atomic_json(directory / "last_plan.json", {"operations": ops, "conflicts": conflicts, "skipped": skipped})
    if conflicts:
        raise ValueError("Aufgaben-Konflikt: " + "; ".join(conflicts))
    return {"directory": directory, "state": state, "device": device, "remote": remote,
            "operations": ops, "skipped": skipped, "svc": svc, "list_id": list_id}


def recover_device_create(directory, state, device, remote):
    pending_path = directory / "pending.json"
    pending = read_json(pending_path)
    op = pending.get("operation", {})
    if op.get("kind") != "write_device" or op.get("rid") is not None:
        return
    backup_path = Path(pending.get("backup", "")).resolve()
    if backup_path.parent != (directory / "backups").resolve():
        return
    backup = read_json(backup_path, {})
    if "device" not in backup:
        return
    tid = op.get("task_id")
    task = remote.get(tid)
    expected = op.get("fields", {})
    if not task or task.get("deleted") or task.get("parent") or task.get("assignmentInfo"):
        return
    if any(t.get("parent") == tid and not t.get("deleted") for t in remote.values()):
        return
    if google_semantic(task) != semantic(expected):
        return
    matches = [rid for rid, fields in device.items() if rid not in backup["device"]
               and tp.matches_written(fields, expected)]
    if len(matches) != 1:
        return
    rid = matches[0]
    for bound_rid, binding in state["bindings"].items():
        if (bound_rid == rid and binding["task_id"] != tid) or (bound_rid != rid and binding["task_id"] == tid):
            return
    state["bindings"][rid] = {"task_id": tid, "fields": device[rid], "semantic": semantic(device[rid])}
    atomic_json(directory / "state.json", state)
    atomic_json(directory / "recovered_operation.json", {"pending": pending, "record_id": rid})
    pending_path.unlink()


def checked_task(svc, list_id, task_id, expected):
    task = svc.tasks().get(tasklist=list_id, task=task_id).execute()
    if not task.get("etag") or task.get("etag") != expected.get("etag"):
        raise RuntimeError("Google-Aufgabe seit der Planung geändert; Abgleich gestoppt.")
    return task


def apply(plan, ser, export, logger=print):
    if plan is None:
        return {"enabled": False}
    svc, list_id = plan["svc"], plan["list_id"]
    directory, state = plan["directory"], plan["state"]
    # Complete new snapshot also catches hierarchy changes before any Tasks mutation.
    if snapshot(svc, list_id) != plan["remote"]:
        raise RuntimeError("Google-Aufgaben seit der Planung verändert.")
    fd = tp.p.open_database(ser, "To Do List")
    if fd is None:
        raise RuntimeError("Zu Erledigen konnte nicht geöffnet werden.")
    stats = {"enabled": True, "write_device": 0, "write_google": 0,
             "delete_device": 0, "delete_google": 0, "skipped": len(plan["skipped"])}
    try:
        records = tp.read_all(ser, fd)
        if device_snapshot({"count": len(records), "records": records}) != plan["device"]:
            raise RuntimeError("IC35-Aufgaben seit der Planung verändert.")
        backup = directory / "backups" / (uuid.uuid4().hex + ".json")
        atomic_json(backup, {k: plan[k] for k in ("state", "device", "remote", "operations")})
        for original_op in plan["operations"]:
            op = copy.deepcopy(original_op)
            kind, rid, tid = op["kind"], op["rid"], op["task_id"]
            task = plan["remote"].get(tid)
            if kind not in ("bind", "forget"):
                if task and not task.get("deleted"):
                    checked_task(svc, list_id, tid, task)
                if rid and rid in plan["device"]:
                    raw = tp.p.read_record_raw_by_id(ser, "To Do List", fd, int(rid))
                    current = tp.p.decode_record("To Do List", raw, -1) if raw else None
                    if tp.values(current) != plan["device"][rid]:
                        raise RuntimeError("IC35-Aufgabe vor Änderung verändert.")
                atomic_json(directory / "pending.json", {"operation": op, "backup": str(backup)})
            if kind == "write_device":
                rec = tp.write(ser, fd, op["fields"], int(rid) if rid else None)
                rid = str(rec["record_id"])
                op["fields"] = tp.values(rec)
            elif kind == "delete_device":
                tp.delete(ser, fd, int(rid))
            elif kind == "write_google":
                desired = semantic(op["fields"])
                if tid:
                    # Patch only changed common fields; preserve Google-only attributes.
                    wanted, previous = body(desired), body(google_semantic(task))
                    patch_body = {k: v for k, v in wanted.items() if v != previous[k]}
                    request = svc.tasks().patch(tasklist=list_id, task=tid, body=patch_body)
                    request.headers["If-Match"] = task["etag"]
                else:
                    request = svc.tasks().insert(tasklist=list_id, body=body(desired))
                # Never retry a possibly successful INSERT automatically.
                task = request.execute(num_retries=0)
                tid = task["id"]
                atomic_json(directory / "pending.json", {"operation": op, "backup": str(backup),
                            "returned_task_id": tid, "returned_task": task})
                task = svc.tasks().get(tasklist=list_id, task=tid).execute()
                if task.get("deleted") or google_semantic(task) != desired:
                    raise RuntimeError("Google-Aufgabe stimmt beim Rücklesen nicht überein.")
            elif kind == "delete_google":
                request = svc.tasks().delete(tasklist=list_id, task=tid)
                request.headers["If-Match"] = task["etag"]
                request.execute(num_retries=0)
                remaining = snapshot(svc, list_id).get(tid)
                if remaining and not remaining.get("deleted"):
                    raise RuntimeError("Google-Aufgabe nach Löschung noch vorhanden.")
            if kind in ("delete_device", "delete_google", "forget"):
                state["bindings"].pop(rid, None)
            else:
                state["bindings"][rid] = {"task_id": tid, "fields": op["fields"],
                                          "semantic": semantic(op["fields"])}
            atomic_json(directory / "state.json", state)
            (directory / "pending.json").unlink(missing_ok=True)
            if kind in stats:
                stats[kind] += 1
            logger(f"Aufgaben: {kind} · IC35 {rid} ↔ Google {tid}")
        final = tp.read_all(ser, fd)
        export["databases"]["To Do List"] = {"count": len(final), "records": final}
        return stats
    finally:
        tp.p.close_database(ser, "To Do List", fd)
