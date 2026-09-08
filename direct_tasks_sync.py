"""Standalone Google Tasks/device run, with no calendar/CardDAV/memo prerequisite."""
from datetime import datetime
from pathlib import Path
import google_tasks_bridge as tasks
import todo_protocol as todo
import ic35_protocol as proto
from memo_sync import acquire_run_lock, atomic_json


def run(port, config, data_dir, credentials_file, open_serial, notify, logger):
    root = Path(data_dir)
    config = dict(config, enabled=True)
    if not config.get("list_id"):
        raise ValueError("Bitte zuerst eine Google-Aufgabenliste auswählen.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report = root / "reports" / f"DirectTasks_{stamp}.json"
    raw_export = root / "exports" / f"IC35_tasks_{stamp}.json"
    for path in (report, raw_export):
        path.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_run_lock(root)
    ser = None
    try:
        notify("sound", "start")
        notify("stage", "1/3 · Google-Aufgaben lesen …")
        svc = tasks.service(credentials_file, root / "google_tasks_token.json")
        remote = tasks.snapshot(svc, config["list_id"])
        logger(f"Google-Liste: {config.get('list_title', config['list_id'])}; {len(remote)} Einträge gelesen.")
        notify("stage", "2/3 · SyncStation drücken: Aufgaben synchronisieren")
        logger("SyncStation einmal drücken. Vollbackup nur über 'Nur Backup'.")
        proto.log = logger
        proto.PORT = port
        ser = open_serial(port)
        if not proto.do_welcome_and_reopen(ser):
            raise RuntimeError("IC35-Handshake fehlgeschlagen.")
        identity = proto.identify(ser)
        if not identity or not proto.power_request(ser):
            raise RuntimeError("IC35 konnte nicht initialisiert werden.")
        auth = proto.authenticate_once(ser, "")
        if auth is None or auth[:2] != b"\x01\x01":
            raise RuntimeError("IC35-Anmeldung fehlgeschlagen.")
        proto.read_sync_info(ser)
        fd = proto.open_database(ser, "To Do List")
        if fd is None:
            raise RuntimeError("Zu Erledigen konnte nicht geöffnet werden.")
        try:
            records = todo.read_all(ser, fd)
        finally:
            proto.close_database(ser, "To Do List", fd)
        export = {"device": identity, "databases": {
            "To Do List": {"count": len(records), "records": records}}}
        atomic_json(raw_export, export)
        # Refresh after the device handshake.
        remote = tasks.snapshot(svc, config["list_id"])
        plan = tasks.prepare(config, root, export, svc, remote)
        logger(f"IC35: {len(records)} Aufgaben; geplant: {len(plan['operations'])} Schritte.")
        stats = tasks.apply(plan, ser, export, logger)
        final = export["databases"]["To Do List"]["records"]
        count = sum(not r.get("deleted") for r in final)
        atomic_json(report, {"list_title": config.get("list_title", ""),
                            "stats": stats, "count": count, "skipped": plan["skipped"],
                            "operations": plan["operations"], "export_after": export})
        proto.disconnect(ser)
        ser.close()
        ser = None
        return {"stats": stats, "count": count, "report": str(report),
                "device": identity}
    finally:
        try:
            if ser is not None:
                ser.close()
        finally:
            lock.close()
