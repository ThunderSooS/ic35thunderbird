from __future__ import annotations

import json
import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request
import xml.etree.ElementTree as ET

USER_NAME = "ic35"
CALENDAR_NAME = "calendar"
ADDRESSBOOK_NAME = "addressbook"


def _esc(value) -> str:
    return (str(value)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
            .replace("\r", "\\n"))


def _field(record: dict, name: str, default=""):
    return record.get("fields", {}).get(name, {}).get("value", default)


def _date8(value) -> str | None:
    s = str(value or "")
    return s[:8] if len(s) >= 8 and s[:8].isdigit() else None


def _time6(value) -> str | None:
    # Real DCS15 1.51 samples show 6 bytes; first four ASCII digits are HHMM.
    s = str(value or "")
    if len(s) >= 4 and s[:4].isdigit():
        hh, mm = int(s[:2]), int(s[2:4])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}{mm:02d}00"
    return None


def contact_to_vcard(record: dict, uid_override: str | None = None) -> str:
    first = str(_field(record, "Vorname", ""))
    last = str(_field(record, "Nachname", ""))
    company = str(_field(record, "Firma", ""))
    fn = " ".join(x for x in (first, last) if x).strip() or company or "IC35 Kontakt"

    lines = [
        "BEGIN:VCARD",
        "VERSION:4.0",
        f"UID:{_esc(uid_override or f"ic35-address-{record.get('record_id', 0):06d}@local")}",
        f"FN:{_esc(fn)}",
        f"N:{_esc(last)};{_esc(first)};;;",
    ]

    if company:
        lines.append(f"ORG:{_esc(company)}")

    street = str(_field(record, "Strasse", ""))
    city = str(_field(record, "Ort", ""))
    region = str(_field(record, "Bundesland", ""))
    postal = str(_field(record, "PLZ", ""))
    country = str(_field(record, "Land", ""))
    if any((street, city, region, postal, country)):
        lines.append(
            f"ADR;TYPE=home:;;{_esc(street)};{_esc(city)};"
            f"{_esc(region)};{_esc(postal)};{_esc(country)}"
        )

    for field_name, tel_type in (
        ("Tel.Privat", "home"),
        ("Tel.Buero", "work"),
        ("Handy", "cell"),
        ("Fax", "fax"),
    ):
        value = str(_field(record, field_name, ""))
        if value:
            lines.append(f"TEL;TYPE={tel_type}:{_esc(value)}")

    for field_name in ("E-Mail1", "E-Mail2"):
        value = str(_field(record, field_name, ""))
        if value:
            lines.append(f"EMAIL:{_esc(value)}")

    url = str(_field(record, "URL", ""))
    if url:
        lines.append(f"URL:{_esc(url)}")

    birthday = str(_field(record, "Geburtstag", ""))
    if len(birthday) >= 8 and birthday[:8].isdigit():
        b = birthday[:8]
        lines.append(f"BDAY:{b[:4]}-{b[4:6]}-{b[6:8]}")
    elif birthday:
        lines.append(f"X-IC35-BIRTHDAY:{_esc(birthday)}")

    notes = str(_field(record, "Notizen", ""))
    if notes:
        lines.append(f"NOTE:{_esc(notes)}")

    category = str(_field(record, "category", ""))
    if category:
        lines.append(f"CATEGORIES:{_esc(category)}")

    lines += [
        f"X-IC35-RECORD-ID:{record.get('record_id', '')}",
        f"X-IC35-FILE-ID:{record.get('file_id', '')}",
        f"X-IC35-CHANGE-FLAG:{record.get('change_flag', '')}",
        "END:VCARD",
    ]
    return "\r\n".join(lines) + "\r\n"


def event_to_ics(record: dict, uid_override: str | None = None) -> str | None:
    start_date = _date8(_field(record, "Start(Datum)", ""))
    end_date = _date8(_field(record, "Ende(Datum)", "")) or start_date
    start_time = _time6(_field(record, "Start(Zeit)", ""))
    end_time = _time6(_field(record, "Ende(Zeit)", "")) or start_time
    if not (start_date and end_date and start_time and end_time):
        return None

    summary = str(_field(record, "Betreff", "")) or "IC35 Termin"
    notes = str(_field(record, "Notizen", ""))
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//IC35 Thunderbird Sync//DCS15//DE",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{_esc(uid_override or f"ic35-schedule-{record.get('record_id', 0):06d}@local")}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start_date}T{start_time}",
        f"DTEND:{end_date}T{end_time}",
        f"SUMMARY:{_esc(summary)}",
    ]
    if notes:
        lines.append(f"DESCRIPTION:{_esc(notes)}")

    # Low 3 bits of AlrmRep encode recurrence: 0 none, 1 day, 2 week,
    # 3 monthly-by-weekday, 4 yearly, 5 monthly-by-monthday.
    repeat_kind = int(_field(record, "AlrmRep", 0) or 0) & 0x07
    interval = max(1, int(_field(record, "RepAlln", 1) or 1))
    freq = {1: "DAILY", 2: "WEEKLY", 3: "MONTHLY", 4: "YEARLY", 5: "MONTHLY"}.get(repeat_kind)
    if freq:
        rule = f"FREQ={freq};INTERVAL={interval}"
        until = _date8(_field(record, "EndRepeat", ""))
        if until:
            rule += f";UNTIL={until}T235959"
        lines.append(f"RRULE:{rule}")

    alarm_code = int(_field(record, "AlrmBef", 0) or 0)
    alarm_minutes = {1: 0, 2: 1, 3: 5, 4: 10, 5: 30, 6: 60, 7: 120, 8: 600, 9: 1440, 10: 2880}
    if alarm_code in alarm_minutes:
        mins = alarm_minutes[alarm_code]
        trigger = "PT0M" if mins == 0 else f"-PT{mins}M"
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_esc(summary)}",
            f"TRIGGER:{trigger}",
            "END:VALARM",
        ]

    lines += [
        f"X-IC35-RECORD-ID:{record.get('record_id', '')}",
        f"X-IC35-FILE-ID:{record.get('file_id', '')}",
        f"X-IC35-CHANGE-FLAG:{record.get('change_flag', '')}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def todo_to_ics(record: dict, uid_override: str | None = None) -> str:
    summary = str(_field(record, "Betreff", "")) or "IC35 Aufgabe"
    notes = str(_field(record, "Notizen", ""))
    start_date = _date8(_field(record, "Start", ""))
    due_date = _date8(_field(record, "Ende", ""))
    completed = int(_field(record, "Erledigt", 0) or 0) == 1
    prio = int(_field(record, "Prioritaet", 1) or 1)
    ical_priority = {0: 9, 1: 5, 2: 1}.get(prio, 5)
    category = str(_field(record, "category", ""))
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//IC35 Thunderbird Sync//DCS15//DE",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTODO",
        f"UID:{_esc(uid_override or f"ic35-todo-{record.get('record_id', 0):06d}@local")}",
        f"DTSTAMP:{dtstamp}",
        f"SUMMARY:{_esc(summary)}",
        f"PRIORITY:{ical_priority}",
        f"STATUS:{'COMPLETED' if completed else 'NEEDS-ACTION'}",
    ]
    if start_date:
        lines.append(f"DTSTART;VALUE=DATE:{start_date}")
    if due_date:
        lines.append(f"DUE;VALUE=DATE:{due_date}")
    if completed:
        lines.append(f"COMPLETED:{dtstamp}")
    if notes:
        lines.append(f"DESCRIPTION:{_esc(notes)}")
    if category:
        lines.append(f"CATEGORIES:{_esc(category)}")
    lines += [
        f"X-IC35-RECORD-ID:{record.get('record_id', '')}",
        f"X-IC35-FILE-ID:{record.get('file_id', '')}",
        f"X-IC35-CHANGE-FLAG:{record.get('change_flag', '')}",
        "END:VTODO",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def ensure_radicale_storage(storage_root: Path) -> dict:
    storage_root = Path(storage_root)
    collection_root = storage_root / "collection-root" / USER_NAME
    calendar = collection_root / CALENDAR_NAME
    addressbook = collection_root / ADDRESSBOOK_NAME
    calendar.mkdir(parents=True, exist_ok=True)
    addressbook.mkdir(parents=True, exist_ok=True)

    (calendar / ".Radicale.props").write_text(json.dumps({
        "tag": "VCALENDAR",
        "D:displayname": "Siemens IC35",
        "C:supported-calendar-component-set": "VEVENT,VTODO",
    }, ensure_ascii=False), encoding="utf-8")
    (addressbook / ".Radicale.props").write_text(json.dumps({
        "tag": "VADDRESSBOOK",
        "D:displayname": "Siemens IC35 Kontakte",
    }, ensure_ascii=False), encoding="utf-8")

    return {"calendar": calendar, "addressbook": addressbook}


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _dav_request(url: str, method: str, body: str | bytes | None = None,
                 content_type: str | None = None, headers: dict | None = None,
                 timeout: float = 8.0):
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    hdrs = {
        "User-Agent": "IC35-Thunderbird-Sync/1.0",
        "Authorization": "Basic " + base64.b64encode(f"{USER_NAME}:".encode("utf-8")).decode("ascii"),
    }
    if headers:
        hdrs.update(headers)
    if content_type:
        hdrs["Content-Type"] = content_type
    req = request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as rsp:
            return rsp.status, dict(rsp.headers), rsp.read()
    except error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _collection_names(base_url: str, suffix: str, logger=print) -> set[str]:
    """List IC35-owned resource filenames with a WebDAV PROPFIND."""
    url = base_url.rstrip("/") + "/" + suffix.strip("/") + "/"
    body = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>"""
    status, _, payload = _dav_request(
        url, "PROPFIND", body,
        content_type="application/xml; charset=utf-8",
        headers={"Depth": "1"},
    )
    if status != 207:
        raise RuntimeError(f"PROPFIND {url} fehlgeschlagen (HTTP {status}).")

    names = set()
    try:
        root = ET.fromstring(payload)
        for href in root.findall(".//{DAV:}href"):
            value = (href.text or "").strip()
            if not value:
                continue
            path = parse.urlparse(value).path
            name = Path(parse.unquote(path)).name
            if name.startswith("ic35-"):
                names.add(name)
    except ET.ParseError as exc:
        raise RuntimeError(f"Ungültige PROPFIND-Antwort von Radicale: {exc}") from exc
    logger(f"DAV-Liste {suffix}: {len(names)} IC35-Einträge gefunden.")
    return names


def _put_resource(url: str, content: str, content_type: str, logger=print):
    status, headers, _ = _dav_request(url, "PUT", content, content_type=content_type)
    if status not in (200, 201, 204):
        raise RuntimeError(f"PUT {url} fehlgeschlagen (HTTP {status}).")
    etag = headers.get("ETag") or headers.get("Etag") or ""
    name = Path(parse.unquote(parse.urlparse(url).path)).name
    if etag:
        logger(f"DAV PUT {name}: HTTP {status}, ETag {etag}")
    else:
        logger(f"DAV PUT {name}: HTTP {status}")
    return etag


def _delete_resource(url: str, logger=print):
    status, _, _ = _dav_request(url, "DELETE")
    if status not in (200, 204, 404):
        raise RuntimeError(f"DELETE {url} fehlgeschlagen (HTTP {status}).")
    name = Path(parse.unquote(parse.urlparse(url).path)).name
    logger(f"DAV DELETE {name}: HTTP {status}")
    return status


def delete_address_resource_dav(base_url: str, resource: str, logger=print) -> int:
    """Delete one CardDAV contact resource through Radicale's HTTP path.

    HTTP 404 is accepted because a Thunderbird-originated deletion normally
    removed the href before the IC35 side is synchronized.  Going through DAV
    whenever the href still exists lets Radicale update history/sync metadata.
    """
    url = (base_url.rstrip("/") + f"/{USER_NAME}/{ADDRESSBOOK_NAME}/" +
           parse.quote(resource))
    return _delete_resource(url, logger)


def publish_export_dav(export: dict, base_url: str, export_dir: Path,
                       state_file: Path, logger=print) -> dict:
    """Publish IC35 data through Radicale's DAV interface.

    Unlike v0.9 this function never writes contact/calendar resources into
    Radicale's collection filesystem directly. Every changed resource uses
    HTTP PUT and every vanished IC35-owned resource uses HTTP DELETE. That
    lets Radicale update ETags, caches and sync metadata in its normal path.
    """
    base_url = base_url.rstrip("/")
    calendar_url = f"{base_url}/{USER_NAME}/{CALENDAR_NAME}/"
    addressbook_url = f"{base_url}/{USER_NAME}/{ADDRESSBOOK_NAME}/"
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    addresses = export.get("databases", {}).get("Addresses", {}).get("records", [])
    schedules = export.get("databases", {}).get("Schedule", {}).get("records", [])
    todos = export.get("databases", {}).get("To Do List", {}).get("records", [])
    memos = export.get("databases", {}).get("Memo", {}).get("records", [])

    desired_ab: dict[str, str] = {}
    desired_cal: dict[str, str] = {}

    for record in addresses:
        if record.get("error") or record.get("deleted"):
            continue
        filename = f"ic35-address-{record.get('record_id', 0):06d}.vcf"
        desired_ab[filename] = contact_to_vcard(record)

    for record in schedules:
        if record.get("error") or record.get("deleted"):
            continue
        content = event_to_ics(record)
        if not content:
            logger(f"WARNUNG: Termin Record {record.get('record_id')} konnte nicht nach ICS konvertiert werden.")
            continue
        filename = f"ic35-schedule-{record.get('record_id', 0):06d}.ics"
        desired_cal[filename] = content

    for record in todos:
        if record.get("error") or record.get("deleted"):
            continue
        filename = f"ic35-todo-{record.get('record_id', 0):06d}.ics"
        desired_cal[filename] = todo_to_ics(record)

    current_ab = _collection_names(base_url, f"{USER_NAME}/{ADDRESSBOOK_NAME}", logger)
    current_cal = _collection_names(base_url, f"{USER_NAME}/{CALENDAR_NAME}", logger)

    etags = {"addressbook": {}, "calendar": {}}

    for filename, content in desired_ab.items():
        etags["addressbook"][filename] = _put_resource(
            addressbook_url + parse.quote(filename), content,
            "text/vcard; charset=utf-8", logger,
        )

    for filename, content in desired_cal.items():
        etags["calendar"][filename] = _put_resource(
            calendar_url + parse.quote(filename), content,
            "text/calendar; charset=utf-8", logger,
        )

    for filename in sorted(current_ab - set(desired_ab)):
        _delete_resource(addressbook_url + parse.quote(filename), logger)

    for filename in sorted(current_cal - set(desired_cal)):
        _delete_resource(calendar_url + parse.quote(filename), logger)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = export_dir / f"IC35_Organizer_Export_{stamp}.json"
    json_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    memo_path = export_dir / f"IC35_Memos_{stamp}.txt"
    memo_lines = []
    for record in memos:
        if record.get("error") or record.get("deleted"):
            continue
        memo_lines += [
            f"Betreff: {_field(record, 'Betreff', '')}",
            f"Kategorie: {_field(record, 'category', '')}",
            f"IC35 Record-ID: {record.get('record_id', '')}",
            "",
            str(_field(record, "Notizen", "")),
            "",
            "-" * 60,
            "",
        ]
    memo_path.write_text("\n".join(memo_lines), encoding="utf-8")

    state = {
        "version": "1.6.0",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "device": export.get("device", ""),
        "base_url": base_url,
        "resources": {
            "addressbook": sorted(desired_ab),
            "calendar": sorted(desired_cal),
        },
        "etags": etags,
        "counts": {
            "addresses": len(desired_ab),
            "schedule_and_todos": len(desired_cal),
            "memos": len([r for r in memos if not r.get("error") and not r.get("deleted")]),
        },
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "contacts": len(desired_ab),
        "calendar_items": len(desired_cal),
        "memos": state["counts"]["memos"],
        "json_export": str(json_path),
        "memo_export": str(memo_path),
        "state_file": str(state_file),
    }


def publish_export_dav_nolist(export: dict, base_url: str, export_dir: Path,
                              state_file: Path, logger=print) -> dict:
    """Publish every current IC35 item with HTTP PUT, without PROPFIND.

    This is the Windows-safe forward-sync path used from v1.1.2 onward.
    We deliberately avoid direct filesystem writes here: Radicale's normal
    upload path updates its item cache/history and therefore its CardDAV/
    CalDAV sync-token state.  We also avoid PROPFIND because an older local
    Radicale collection produced HTTP 500 on PROPFIND in earlier test builds.

    Deletions are intentionally NOT performed in this mode.  Until explicit
    deletion/conflict handling is implemented, preserving an old resource is
    safer than deleting one because of an incomplete mapping.
    """
    base_url = base_url.rstrip("/")
    calendar_url = f"{base_url}/{USER_NAME}/{CALENDAR_NAME}/"
    addressbook_url = f"{base_url}/{USER_NAME}/{ADDRESSBOOK_NAME}/"
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    addresses = export.get("databases", {}).get("Addresses", {}).get("records", [])
    schedules = export.get("databases", {}).get("Schedule", {}).get("records", [])
    todos = export.get("databases", {}).get("To Do List", {}).get("records", [])
    memos = export.get("databases", {}).get("Memo", {}).get("records", [])

    # Load only bindings that still point at the same current IC35 record-id.
    # If an IC35 record-id changed between sessions, fall back to a canonical
    # href.  The new canonical resource is then tracked from this sync onward.
    try:
        old_state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        old_state = {}
    old_bindings = old_state.get("bindings", {}).get("addressbook", {})
    old_cal_bindings = old_state.get("bindings", {}).get("calendar", {})

    desired_ab: dict[str, str] = {}
    desired_cal: dict[str, str] = {}
    bindings: dict[str, dict] = {}
    cal_bindings: dict[str, dict] = {}

    for record in addresses:
        if record.get("error") or record.get("deleted"):
            continue
        rid = int(record.get("record_id", 0) or 0)
        binding = old_bindings.get(str(rid), {}) if isinstance(old_bindings, dict) else {}
        filename = str(binding.get("resource") or f"ic35-address-{rid:06d}.vcf")
        uid = str(binding.get("uid") or "") or None
        content = contact_to_vcard(record, uid_override=uid)
        desired_ab[filename] = content
        try:
            sem = _parse_vcard_semantic(content)
            out_uid = str(sem.get("uid") or "")
        except Exception:
            out_uid = str(uid or "")
        bindings[str(rid)] = {"resource": filename, "uid": out_uid}

    for record in schedules:
        if record.get("error") or record.get("deleted"):
            continue
        rid = int(record.get("record_id", 0) or 0)
        key = f"VEVENT:{rid}"
        binding = old_cal_bindings.get(key, {}) if isinstance(old_cal_bindings, dict) else {}
        filename = str(binding.get("resource") or f"ic35-schedule-{rid:06d}.ics")
        uid = str(binding.get("uid") or "") or None
        content = event_to_ics(record, uid_override=uid)
        if not content:
            logger(f"WARNUNG: Termin Record {record.get('record_id')} konnte nicht nach ICS konvertiert werden.")
            continue
        desired_cal[filename] = content
        sem = _parse_ical_semantic(content)
        cal_bindings[key] = {"component": "VEVENT", "record_id": rid,
                             "resource": filename, "uid": str(sem.get("uid") or "")}

    for record in todos:
        if record.get("error") or record.get("deleted"):
            continue
        rid = int(record.get("record_id", 0) or 0)
        key = f"VTODO:{rid}"
        binding = old_cal_bindings.get(key, {}) if isinstance(old_cal_bindings, dict) else {}
        filename = str(binding.get("resource") or f"ic35-todo-{rid:06d}.ics")
        uid = str(binding.get("uid") or "") or None
        content = todo_to_ics(record, uid_override=uid)
        desired_cal[filename] = content
        sem = _parse_ical_semantic(content)
        cal_bindings[key] = {"component": "VTODO", "record_id": rid,
                             "resource": filename, "uid": str(sem.get("uid") or "")}

    etags = {"addressbook": {}, "calendar": {}}
    logger("=== DAV-Publish: IC35 → Radicale ===")
    logger("Jede Ressource wird per HTTP PUT durch Radicale geschrieben; kein direkter Storage-Write.")

    for filename, content in desired_ab.items():
        etags["addressbook"][filename] = _put_resource(
            addressbook_url + parse.quote(filename), content,
            "text/vcard; charset=utf-8", logger,
        )

    for filename, content in desired_cal.items():
        etags["calendar"][filename] = _put_resource(
            calendar_url + parse.quote(filename), content,
            "text/calendar; charset=utf-8", logger,
        )

    logger("DAV-Publish: keine Kalender-DELETE-Operationen in v1.6.0.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = export_dir / f"IC35_Organizer_Export_{stamp}.json"
    json_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    memo_path = export_dir / f"IC35_Memos_{stamp}.txt"
    memo_lines = []
    for record in memos:
        if record.get("error") or record.get("deleted"):
            continue
        memo_lines += [
            f"Betreff: {_field(record, 'Betreff', '')}",
            f"Kategorie: {_field(record, 'category', '')}",
            f"IC35 Record-ID: {record.get('record_id', '')}", "",
            str(_field(record, "Notizen", "")), "", "-" * 60, "",
        ]
    memo_path.write_text("\n".join(memo_lines), encoding="utf-8")

    contact_baseline = {}
    for rid, binding in bindings.items():
        resource = binding.get("resource", "")
        content = desired_ab.get(resource)
        if content:
            contact_baseline[str(rid)] = {
                "resource": resource,
                "uid": binding.get("uid", ""),
                "semantic": _clean_semantic(_parse_vcard_semantic(content)),
            }

    calendar_baseline = {}
    for key, binding in cal_bindings.items():
        resource = binding.get("resource", "")
        content = desired_cal.get(resource)
        if content:
            calendar_baseline[key] = {
                "resource": resource,
                "uid": binding.get("uid", ""),
                "semantic": _clean_semantic(_parse_ical_semantic(content)),
            }

    state = {
        "version": "1.6.0",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "device": export.get("device", ""),
        "base_url": base_url,
        "resources": {
            "addressbook": sorted(desired_ab),
            "calendar": sorted(desired_cal),
        },
        "bindings": {"addressbook": bindings, "calendar": cal_bindings},
        "contact_baseline": contact_baseline,
        "calendar_baseline": calendar_baseline,
        "etags": etags,
        "counts": {
            "addresses": len(desired_ab),
            "schedule_and_todos": len(desired_cal),
            "memos": len([r for r in memos if not r.get("error") and not r.get("deleted")]),
        },
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "contacts": len(desired_ab),
        "calendar_items": len(desired_cal),
        "memos": state["counts"]["memos"],
        "json_export": str(json_path),
        "memo_export": str(memo_path),
        "state_file": str(state_file),
    }


def _vcard_with_ic35_identity(text: str, record_id: int, file_id: int = 0x05) -> str:
    """Return a vCard with bridge identity properties replaced atomically."""
    lines = _unfold_lines(text)
    filtered = []
    inserted = False
    for line in lines:
        upper = line.upper()
        if (upper.startswith("X-IC35-RECORD-ID:") or
                upper.startswith("X-IC35-FILE-ID:") or
                upper.startswith("X-IC35-CHANGE-FLAG:")):
            continue
        if upper == "END:VCARD" and not inserted:
            filtered.extend([
                f"X-IC35-RECORD-ID:{int(record_id)}",
                f"X-IC35-FILE-ID:{int(file_id)}",
                "X-IC35-CHANGE-FLAG:0",
            ])
            inserted = True
        filtered.append(line)
    if not inserted:
        raise RuntimeError("Ungültige vCard ohne END:VCARD.")
    return "\r\n".join(filtered) + "\r\n"


def remember_address_binding(state_file: Path, record_id: int, resource: str,
                             uid: str = "", semantic: dict | None = None,
                             logger=print) -> dict:
    """Persist IC35 Record-ID <-> CardDAV href mapping without touching cache."""
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    resources = state.setdefault("resources", {})
    ab = set(resources.get("addressbook", []))
    ab.add(resource)
    resources["addressbook"] = sorted(ab)
    bindings = state.setdefault("bindings", {}).setdefault("addressbook", {})
    bindings[str(int(record_id))] = {"resource": resource, "uid": uid or ""}
    if semantic is not None:
        baseline = state.setdefault("contact_baseline", {})
        baseline[str(int(record_id))] = {
            "resource": resource,
            "uid": uid or "",
            "semantic": _clean_semantic(semantic),
        }
    state["version"] = "1.5.0"
    state["last_binding_update"] = datetime.now(timezone.utc).isoformat()
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)
    logger(f"STATE BIND Record-ID {record_id} <-> {resource} (UID={uid!r})")
    return state


def mark_address_resource_ic35_id_dav(base_url: str, resource: str, content: str,
                                       record_id: int, file_id: int = 0x05,
                                       logger=print) -> str:
    """Mark a Thunderbird-created vCard through Radicale's normal HTTP PUT path."""
    updated = _vcard_with_ic35_identity(content, record_id, file_id)
    url = (base_url.rstrip("/") + f"/{USER_NAME}/{ADDRESSBOOK_NAME}/" +
           parse.quote(resource))
    _put_resource(url, updated, "text/vcard; charset=utf-8", logger)
    logger(f"DAV MARK {resource}: X-IC35-RECORD-ID={record_id}, FILE-ID={file_id}")
    return updated


def publish_contacts_dav_nolist(export: dict, base_url: str, state_file: Path,
                                 logger=print) -> dict:
    """Publish current IC35 contacts only, preserving Thunderbird href/UID bindings.

    No PROPFIND, no DELETE, no direct collection-file writes, and no cache deletion.
    Calendar collections are intentionally untouched by this contact-only sync.
    """
    base_url = base_url.rstrip("/")
    addressbook_url = f"{base_url}/{USER_NAME}/{ADDRESSBOOK_NAME}/"
    state_file = Path(state_file)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    old_bindings = state.get("bindings", {}).get("addressbook", {})
    if not isinstance(old_bindings, dict):
        old_bindings = {}

    desired = {}
    bindings = {}
    baseline = {}
    etags = {}
    for record in export.get("databases", {}).get("Addresses", {}).get("records", []):
        if record.get("error") or record.get("deleted"):
            continue
        rid = int(record.get("record_id", 0) or 0)
        old = old_bindings.get(str(rid), {}) if isinstance(old_bindings.get(str(rid), {}), dict) else {}
        resource = str(old.get("resource") or f"ic35-address-{rid:06d}.vcf")
        uid = str(old.get("uid") or "") or None
        content = contact_to_vcard(record, uid_override=uid)
        desired[resource] = content
        sem = _parse_vcard_semantic(content)
        out_uid = str(sem.get("uid") or "")
        bindings[str(rid)] = {"resource": resource, "uid": out_uid}
        baseline[str(rid)] = {
            "resource": resource, "uid": out_uid,
            "semantic": _clean_semantic(sem),
        }

    logger("=== DAV-Publish Kontakte: IC35 → Radicale ===")
    for resource, content in desired.items():
        etags[resource] = _put_resource(
            addressbook_url + parse.quote(resource), content,
            "text/vcard; charset=utf-8", logger,
        )
    logger("Kontakt-Publish: keine DELETE-Operationen.")

    resources = state.setdefault("resources", {})
    resources["addressbook"] = sorted(desired)
    state.setdefault("bindings", {})["addressbook"] = bindings
    state["contact_baseline"] = baseline
    state.setdefault("etags", {})["addressbook"] = etags
    state["version"] = "1.5.0"
    state["synced_at"] = datetime.now(timezone.utc).isoformat()
    state["device"] = export.get("device", state.get("device", ""))
    state["base_url"] = base_url
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)
    return {"contacts": len(desired), "bindings": bindings, "baseline": baseline}


def analyze_contact_two_way(export: dict, dav_snapshot: dict, state_file: Path | None = None,
                            logger=print) -> dict:
    """Classify safe CREATE/UPDATE/DELETE operations using a shared baseline.

    A deletion is automatic only when the item was known in the last successful
    baseline and the surviving side is still equal to that baseline.  A
    delete-vs-modify situation is a conflict and is never resolved
    automatically.  This makes normal v1.5 contact deletion symmetric:
    Thunderbird deletion -> verified IC35 DELETE, IC35 deletion -> DAV DELETE.
    """
    reverse = analyze_reverse_dry_run(export, dav_snapshot, state_file, logger=logger)
    try:
        state = json.loads(Path(state_file).read_text(encoding="utf-8")) if state_file and Path(state_file).exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    baseline = state.get("contact_baseline", {})
    if not isinstance(baseline, dict):
        baseline = {}

    expected_contacts = _expected_semantic_from_export(export).get("addressbook", {})
    plan = {
        "new_to_ic35": [],
        "update_to_ic35": [],
        "publish_from_ic35": [],
        "delete_to_ic35": [],          # deleted in Thunderbird, unchanged on IC35
        "delete_from_carddav": [],     # deleted on IC35, unchanged in Thunderbird
        "unchanged": list(reverse["contacts"]["unchanged"]),
        "delete_locked": [],
        "conflicts": [],
        "calendar": reverse["calendar"],
        "warnings": list(reverse.get("warnings", [])),
        "raw_reverse": reverse,
    }

    # Missing CardDAV resource while the IC35 record still exists:
    # accept as Thunderbird-side deletion only if the device copy is unchanged
    # from the common baseline.
    for item in reverse["contacts"]["delete"]:
        rid_i = int(item.get("record_id", 0) or 0)
        rid = str(rid_i)
        base_entry = baseline.get(rid, {}) if isinstance(baseline.get(rid, {}), dict) else {}
        base_sem = _clean_semantic(base_entry.get("semantic", {})) if base_entry.get("semantic") else None
        info = expected_contacts.get(("contact", rid_i), {})
        dev_sem = _clean_semantic(info.get("semantic", {})) if info.get("semantic") else None
        if base_sem is None or dev_sem is None:
            locked = dict(item)
            locked["direction"] = "deleted_in_thunderbird"
            locked["reason"] = "kein eindeutiger gemeinsamer Baseline-Stand"
            plan["delete_locked"].append(locked)
            logger(f"LÖSCHUNG GESPERRT Record-ID {rid}: Thunderbird-Ressource fehlt, aber Baseline ist nicht eindeutig.")
        elif dev_sem == base_sem:
            safe = dict(item)
            safe["baseline_semantic"] = base_sem
            safe["ic35"] = dev_sem
            plan["delete_to_ic35"].append(safe)
            logger(f"ZWEIWEG DELETE Record-ID {rid}: nur in Thunderbird gelöscht -> DELETE auf IC35 freigegeben.")
        else:
            conflict = dict(item)
            conflict["reason"] = "in Thunderbird gelöscht, auf IC35 seit letztem Sync geändert"
            conflict["baseline"] = base_sem
            conflict["ic35"] = dev_sem
            plan["conflicts"].append(conflict)
            logger(f"KONFLIKT Record-ID {rid}: Thunderbird hat gelöscht, IC35 wurde seit der Baseline geändert.")

    # CardDAV resource still exists but its former IC35 Record-ID is gone:
    # accept as IC35-side deletion only if Thunderbird still equals baseline.
    for item in reverse["contacts"]["new"]:
        former = item.get("former_record_id")
        if former is None:
            plan["new_to_ic35"].append(item)
            continue
        rid_i = int(former)
        rid = str(rid_i)
        base_entry = baseline.get(rid, {}) if isinstance(baseline.get(rid, {}), dict) else {}
        base_sem = _clean_semantic(base_entry.get("semantic", {})) if base_entry.get("semantic") else None
        tb_sem = _clean_semantic(item.get("semantic", {}))
        if base_sem is None:
            locked = dict(item)
            locked["record_id"] = rid_i
            locked["direction"] = "deleted_on_ic35_or_stale_id"
            locked["reason"] = "kein eindeutiger gemeinsamer Baseline-Stand"
            plan["delete_locked"].append(locked)
            logger(
                f"LÖSCHUNG GESPERRT: {item.get('resource')} trägt ehemalige IC35 Record-ID {rid_i}; "
                "ohne Baseline wird weder neu angelegt noch gelöscht."
            )
        elif tb_sem == base_sem:
            safe = dict(item)
            safe["record_id"] = rid_i
            safe["baseline_semantic"] = base_sem
            plan["delete_from_carddav"].append(safe)
            logger(f"ZWEIWEG DELETE Record-ID {rid}: nur auf IC35 gelöscht -> CardDAV DELETE freigegeben.")
        else:
            conflict = dict(item)
            conflict["record_id"] = rid_i
            conflict["reason"] = "auf IC35 gelöscht, in Thunderbird seit letztem Sync geändert"
            conflict["baseline"] = base_sem
            conflict["thunderbird"] = tb_sem
            plan["conflicts"].append(conflict)
            logger(f"KONFLIKT Record-ID {rid}: IC35 hat gelöscht, Thunderbird wurde seit der Baseline geändert.")

    # Existing records that differ on both sides use the same baseline rule.
    for item in reverse["contacts"]["update"]:
        rid = str(int(item.get("record_id", 0)))
        base_entry = baseline.get(rid, {}) if isinstance(baseline.get(rid, {}), dict) else {}
        base_sem = _clean_semantic(base_entry.get("semantic", {})) if base_entry.get("semantic") else None
        dev_sem = _clean_semantic(item.get("ic35", {}))
        tb_sem = _clean_semantic(item.get("thunderbird", {}))
        if base_sem is None:
            conflict = dict(item)
            conflict["reason"] = "kein gemeinsamer v1.5-Baseline-Stand vorhanden"
            plan["conflicts"].append(conflict)
            logger(f"KONFLIKT Record-ID {rid}: kein Baseline-Stand; keine automatische Richtung geraten.")
            continue
        dev_changed = dev_sem != base_sem
        tb_changed = tb_sem != base_sem
        if tb_changed and not dev_changed:
            plan["update_to_ic35"].append(item)
            logger(f"ZWEIWEG Record-ID {rid}: nur Thunderbird geändert -> UPDATE IC35.")
        elif dev_changed and not tb_changed:
            plan["publish_from_ic35"].append(item)
            logger(f"ZWEIWEG Record-ID {rid}: nur IC35 geändert -> Publish nach Thunderbird.")
        elif dev_sem == tb_sem:
            plan["unchanged"].append({
                "record_id": int(rid), "resource": item.get("resource", ""),
                "uid": item.get("uid", ""), "converged": True,
            })
            logger(f"ZWEIWEG Record-ID {rid}: beide Seiten bereits identisch -> Baseline erneuern.")
        else:
            conflict = dict(item)
            conflict["reason"] = "beide Seiten seit letztem Sync unterschiedlich geändert"
            plan["conflicts"].append(conflict)
            logger(f"KONFLIKT Record-ID {rid}: IC35 und Thunderbird wurden beide unterschiedlich geändert.")

    logger(
        "Kontakt-Zweiweg-Plan: "
        f"neu→IC35={len(plan['new_to_ic35'])}, "
        f"update→IC35={len(plan['update_to_ic35'])}, "
        f"delete→IC35={len(plan['delete_to_ic35'])}, "
        f"IC35→TB={len(plan['publish_from_ic35'])}, "
        f"delete→CardDAV={len(plan['delete_from_carddav'])}, "
        f"Konflikte={len(plan['conflicts'])}, "
        f"Löschungen gesperrt={len(plan['delete_locked'])}."
    )
    return plan

# ---------------------------------------------------------------------------
# Thunderbird -> IC35 reverse-sync analysis (DRY RUN only in v1.0)
# ---------------------------------------------------------------------------

def _unfold_lines(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unesc(value: str) -> str:
    # Enough for the vCard/iCalendar strings relevant to the IC35 fields.
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _prop_name_and_params(head: str):
    parts = head.split(";")
    name = parts[0].split(".")[-1].upper()
    params = {}
    for item in parts[1:]:
        if "=" in item:
            key, value = item.split("=", 1)
            params[key.upper()] = [v.strip('"').upper() for v in value.split(",")]
        else:
            params.setdefault("TYPE", []).append(item.upper())
    return name, params


def _parse_vcard_semantic(text: str) -> dict:
    result = {
        "kind": "contact", "record_id": None, "uid": "",
        "first": "", "last": "", "company": "",
        "home_phone": "", "work_phone": "", "cell": "", "fax": "",
        "street": "", "city": "", "postal": "", "region": "", "country": "",
        "email1": "", "email2": "", "url": "", "birthday": "", "notes": "",
        "category": "",
    }
    emails = []
    for line in _unfold_lines(text):
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, params = _prop_name_and_params(head)
        value = _unesc(value)
        if name == "UID":
            result["uid"] = value.strip()
        elif name == "X-IC35-RECORD-ID":
            try: result["record_id"] = int(value.strip())
            except ValueError: pass
        elif name == "N":
            comps = value.split(";") + [""] * 5
            result["last"], result["first"] = comps[0], comps[1]
        elif name == "ORG":
            result["company"] = value.split(";")[0]
        elif name == "ADR":
            comps = value.split(";") + [""] * 7
            result["street"], result["city"], result["region"], result["postal"], result["country"] = comps[2:7]
        elif name == "TEL":
            tel = value.strip()
            if tel.lower().startswith("tel:"):
                tel = tel[4:]
            types = set(params.get("TYPE", []))
            if "CELL" in types:
                result["cell"] = tel
            elif "FAX" in types:
                result["fax"] = tel
            elif "WORK" in types:
                result["work_phone"] = tel
            elif "HOME" in types or not types:
                result["home_phone"] = tel
        elif name == "EMAIL":
            emails.append(value.strip())
        elif name == "URL":
            result["url"] = value.strip()
        elif name == "BDAY":
            result["birthday"] = "".join(ch for ch in value if ch.isdigit())[:8]
        elif name == "X-IC35-BIRTHDAY" and not result["birthday"]:
            result["birthday"] = value.strip()
        elif name == "NOTE":
            result["notes"] = value
        elif name == "CATEGORIES":
            result["category"] = value.split(",")[0]
    if emails:
        result["email1"] = emails[0]
    if len(emails) > 1:
        result["email2"] = emails[1]
    return result


def contact_semantic_to_ic35_fields(sem: dict) -> dict:
    """Map one CardDAV vCard semantic object to the 21 IC35 Address fields."""
    category_raw = str(sem.get("category", "") or "").strip()
    category_key = category_raw.lower()
    if category_key.startswith("business"):
        category_id, category = 0x06, "Business"
    elif category_key.startswith("personal"):
        category_id, category = 0x0B, "Personal"
    else:
        category_id, category = 0x0C, "Unfiled"

    birthday = "".join(ch for ch in str(sem.get("birthday", "") or "") if ch.isdigit())[:8]
    return {
        "Vorname": sem.get("first", ""),
        "Nachname": sem.get("last", ""),
        "Firma": sem.get("company", ""),
        "Tel.Privat": sem.get("home_phone", ""),
        "Tel.Buero": sem.get("work_phone", ""),
        "Handy": sem.get("cell", ""),
        "Fax": sem.get("fax", ""),
        "Strasse": sem.get("street", ""),
        "Ort": sem.get("city", ""),
        "PLZ": sem.get("postal", ""),
        "Bundesland": sem.get("region", ""),
        "Land": sem.get("country", ""),
        "E-Mail1": sem.get("email1", ""),
        "E-Mail2": sem.get("email2", ""),
        "URL": sem.get("url", ""),
        "Geburtstag": birthday,
        "Notizen": sem.get("notes", ""),
        "category-id": category_id,
        "(def.)1": "",
        "(def.)2": "",
        "category": category,
    }



def _ical_local_datetime(value: str, label: str) -> tuple[str, str]:
    """Convert a simple local iCalendar DATE-TIME to IC35 YYYYMMDD + HHMMSS.

    v1.6.0 deliberately refuses UTC DATE-TIME and all-day DATE values.  The
    IC35 stores wall-clock local time without a timezone, so silently writing a
    UTC value would be dangerous.  TZID parameters are removed by the parser;
    their value is already a local wall-clock DATE-TIME and is accepted.
    """
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raise ValueError(f"{label}: UTC-Zeit ({raw}) wird im ersten Kalender-Schreibtest nicht unterstützt.")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 8:
        raise ValueError(f"{label}: Ganztägige Termine werden im ersten Kalender-Schreibtest noch nicht unterstützt.")
    if len(digits) < 12:
        raise ValueError(f"{label}: ungültige/fehlende iCalendar-Zeit {raw!r}.")
    date = digits[:8]
    hhmm = digits[8:12]
    sec = digits[12:14] if len(digits) >= 14 else "00"
    try:
        dt = datetime.strptime(date + hhmm + sec, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError(f"{label}: ungültige DATE-TIME {raw!r}.") from exc
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M%S")


def event_semantic_to_ic35_fields(sem: dict) -> dict:
    """Map one simple Thunderbird VEVENT to the 10 IC35 Schedule fields.

    This is intentionally conservative for the first real write test:
    - VEVENT only (no VTODO)
    - timed local event only (no all-day, no UTC-Z values)
    - no recurrence
    - no reminder/alarm
    """
    if str(sem.get("component", "")).upper() != "VEVENT":
        raise ValueError("Der erste Kalender-Schreibtest erlaubt nur VEVENT-Termine, keine Aufgaben.")
    if sem.get("rrule"):
        raise ValueError("Wiederholende Termine werden im ersten Kalender-Schreibtest noch nicht geschrieben.")
    if sem.get("alarm_trigger"):
        raise ValueError("Termine mit Erinnerung werden im ersten Kalender-Schreibtest noch nicht geschrieben.")
    subject = str(sem.get("summary", "") or "").strip()
    if not subject:
        raise ValueError("Der IC35 benötigt einen Betreff für den Termin.")
    sd, st = _ical_local_datetime(sem.get("dtstart", ""), "DTSTART")
    ed, et = _ical_local_datetime(sem.get("dtend", ""), "DTEND")
    if (ed, et) < (sd, st):
        raise ValueError("DTEND liegt vor DTSTART.")
    return {
        "Betreff": subject,
        "Start(Datum)": sd,
        "Start(Zeit)": st,
        "Ende(Zeit)": et,
        "AlrmBef": 0x00,
        "Notizen": str(sem.get("description", "") or ""),
        # Historic ic35link initializes a no-alarm/no-repeat event with both
        # output channels disabled and repeat kind 0.
        "AlrmRep": 0xC0,
        "Ende(Datum)": ed,
        "EndRepeat": "",
        "RepAlln": 0x00,
    }


def _ical_with_ic35_identity(text: str, record_id: int, file_id: int = 0x08) -> str:
    lines = _unfold_lines(text)
    filtered = []
    inserted = False
    component = None
    for line in lines:
        upper = line.upper()
        if upper in ("BEGIN:VEVENT", "BEGIN:VTODO"):
            component = upper.split(":", 1)[1]
        if (upper.startswith("X-IC35-RECORD-ID:") or
                upper.startswith("X-IC35-FILE-ID:") or
                upper.startswith("X-IC35-CHANGE-FLAG:")):
            continue
        if component and upper == f"END:{component}" and not inserted:
            filtered.extend([
                f"X-IC35-RECORD-ID:{int(record_id)}",
                f"X-IC35-FILE-ID:{int(file_id)}",
                "X-IC35-CHANGE-FLAG:0",
            ])
            inserted = True
        filtered.append(line)
    if not inserted:
        raise RuntimeError("Ungültige iCalendar-Ressource ohne VEVENT/VTODO-Ende.")
    return "\r\n".join(filtered) + "\r\n"


def mark_calendar_resource_ic35_id_dav(base_url: str, resource: str, content: str,
                                        record_id: int, file_id: int = 0x08,
                                        logger=print) -> str:
    """Mark one Thunderbird-created calendar resource via Radicale HTTP PUT."""
    updated = _ical_with_ic35_identity(content, record_id, file_id)
    url = (base_url.rstrip("/") + f"/{USER_NAME}/{CALENDAR_NAME}/" +
           parse.quote(resource))
    _put_resource(url, updated, "text/calendar; charset=utf-8", logger)
    logger(f"DAV MARK CAL {resource}: X-IC35-RECORD-ID={record_id}, FILE-ID={file_id}")
    return updated


def remember_calendar_binding(state_file: Path, component: str, record_id: int,
                              resource: str, uid: str = "", semantic: dict | None = None,
                              logger=print) -> dict:
    """Persist a namespaced Schedule/ToDo Record-ID <-> CalDAV href binding."""
    component = str(component or "").upper()
    if component not in ("VEVENT", "VTODO"):
        raise ValueError(f"Ungültiger Kalender-Komponententyp: {component!r}")
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    resources = state.setdefault("resources", {})
    cal = set(resources.get("calendar", []))
    cal.add(resource)
    resources["calendar"] = sorted(cal)
    key = f"{component}:{int(record_id)}"
    bindings = state.setdefault("bindings", {}).setdefault("calendar", {})
    bindings[key] = {
        "component": component, "record_id": int(record_id),
        "resource": resource, "uid": uid or "",
    }
    if semantic is not None:
        baseline = state.setdefault("calendar_baseline", {})
        baseline[key] = {
            "resource": resource, "uid": uid or "",
            "semantic": _clean_semantic(semantic),
        }
    state["version"] = "1.6.0"
    state["last_binding_update"] = datetime.now(timezone.utc).isoformat()
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)
    logger(f"STATE CAL BIND {component} Record-ID {record_id} <-> {resource} (UID={uid!r})")
    return state

def mark_address_resource_ic35_id(storage_root: Path, resource: str, record_id: int,
                                   file_id: int = 0x05, logger=print) -> Path:
    """Add/replace X-IC35 identity fields on a Thunderbird-created vCard."""
    paths = ensure_radicale_storage(storage_root)
    path = paths["addressbook"] / resource
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"CardDAV-Ressource nicht gefunden: {resource}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = _unfold_lines(text)
    filtered = []
    inserted = False
    for line in lines:
        upper = line.upper()
        if upper.startswith("X-IC35-RECORD-ID:") or upper.startswith("X-IC35-FILE-ID:") or upper.startswith("X-IC35-CHANGE-FLAG:"):
            continue
        if upper == "END:VCARD" and not inserted:
            filtered.extend([
                f"X-IC35-RECORD-ID:{record_id}",
                f"X-IC35-FILE-ID:{file_id}",
                "X-IC35-CHANGE-FLAG:0",
            ])
            inserted = True
        filtered.append(line)
    if not inserted:
        raise RuntimeError(f"Ungültige vCard ohne END:VCARD: {resource}")
    _atomic_write(path, "\r\n".join(filtered) + "\r\n")
    logger(f"Storage MARK {resource}: X-IC35-RECORD-ID={record_id}, FILE-ID={file_id}")
    # IMPORTANT: Do not delete .Radicale.cache here.  Normal sync must keep
    # history/sync-token state intact.  Cache cleanup is reserved for the
    # explicit manual repair function only.
    return path


def _parse_ical_semantic(text: str) -> dict:
    lines = _unfold_lines(text)
    component = None
    result = {"kind": "calendar", "component": "", "record_id": None, "uid": ""}
    in_alarm = False
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            component = "VEVENT"; result["component"] = component; continue
        if upper == "BEGIN:VTODO":
            component = "VTODO"; result["component"] = component; continue
        if upper == "BEGIN:VALARM":
            in_alarm = True; continue
        if upper == "END:VALARM":
            in_alarm = False; continue
        if not component or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, params = _prop_name_and_params(head)
        value = _unesc(value).strip()
        if in_alarm:
            if name == "TRIGGER": result["alarm_trigger"] = value
            continue
        if name == "UID": result["uid"] = value
        elif name == "X-IC35-RECORD-ID":
            try: result["record_id"] = int(value)
            except ValueError: pass
        elif name == "SUMMARY": result["summary"] = value
        elif name == "DESCRIPTION": result["description"] = value
        elif name == "DTSTART": result["dtstart"] = value
        elif name == "DTEND": result["dtend"] = value
        elif name == "DUE": result["due"] = value
        elif name == "RRULE": result["rrule"] = value.upper()
        elif name == "PRIORITY": result["priority"] = value
        elif name == "STATUS": result["status"] = value.upper()
        elif name == "CATEGORIES": result["category"] = value
    return result


def _semantic_content(text: str, filename: str = "") -> dict:
    upper = text.upper()
    if "BEGIN:VCARD" in upper or filename.lower().endswith(".vcf"):
        return _parse_vcard_semantic(text)
    return _parse_ical_semantic(text)


def _all_collection_resources(base_url: str, suffix: str, logger=print) -> dict[str, str]:
    """Return direct DAV child resource names and ETags (not just ic35-* names)."""
    url = base_url.rstrip("/") + "/" + suffix.strip("/") + "/"
    body = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:"><D:prop><D:getetag/><D:resourcetype/></D:prop></D:propfind>"""
    status, _, payload = _dav_request(url, "PROPFIND", body,
        content_type="application/xml; charset=utf-8", headers={"Depth": "1"})
    if status != 207:
        raise RuntimeError(f"PROPFIND {url} fehlgeschlagen (HTTP {status}).")
    resources = {}
    root = ET.fromstring(payload)
    ns = {"D": "DAV:"}
    for response in root.findall("D:response", ns):
        href = response.find("D:href", ns)
        if href is None or not (href.text or "").strip():
            continue
        path = parse.urlparse((href.text or "").strip()).path
        name = Path(parse.unquote(path.rstrip("/"))).name
        # Skip the collection itself and collection children.
        rtypes = response.findall(".//D:resourcetype/*", ns)
        if rtypes:
            continue
        if not name or name.startswith("."):
            continue
        etag_node = response.find(".//D:getetag", ns)
        resources[name] = (etag_node.text or "").strip() if etag_node is not None else ""
    logger(f"DAV-Snapshot {suffix}: {len(resources)} Ressourcen gefunden.")
    return resources


def fetch_dav_snapshot(base_url: str, logger=print) -> dict:
    """Read CardDAV/CalDAV resources without modifying the server."""
    base_url = base_url.rstrip("/")
    result = {"addressbook": {}, "calendar": {}}
    for key, collection in (("addressbook", ADDRESSBOOK_NAME), ("calendar", CALENDAR_NAME)):
        suffix = f"{USER_NAME}/{collection}"
        collection_url = f"{base_url}/{suffix}/"
        listed = _all_collection_resources(base_url, suffix, logger)
        for name, etag in listed.items():
            status, _, payload = _dav_request(collection_url + parse.quote(name), "GET")
            if status != 200:
                logger(f"WARNUNG: DAV GET {name}: HTTP {status}")
                continue
            text = payload.decode("utf-8", errors="replace")
            result[key][name] = {
                "etag": etag,
                "content": text,
                "semantic": _semantic_content(text, name),
            }
            logger(f"DAV GET {name}: HTTP 200")
    return result


def _expected_semantic_from_export(export: dict) -> dict:
    expected = {"addressbook": {}, "calendar": {}}
    db = export.get("databases", {})
    for r in db.get("Addresses", {}).get("records", []):
        if r.get("error") or r.get("deleted"): continue
        rid = r.get("record_id")
        content = contact_to_vcard(r)
        expected["addressbook"][("contact", rid)] = {
            "filename": f"ic35-address-{rid:06d}.vcf", "semantic": _parse_vcard_semantic(content), "record": r,
        }
    for r in db.get("Schedule", {}).get("records", []):
        if r.get("error") or r.get("deleted"): continue
        rid = r.get("record_id")
        content = event_to_ics(r)
        if content:
            expected["calendar"][("VEVENT", rid)] = {
                "filename": f"ic35-schedule-{rid:06d}.ics", "semantic": _parse_ical_semantic(content), "record": r,
            }
    for r in db.get("To Do List", {}).get("records", []):
        if r.get("error") or r.get("deleted"): continue
        rid = r.get("record_id")
        content = todo_to_ics(r)
        expected["calendar"][("VTODO", rid)] = {
            "filename": f"ic35-todo-{rid:06d}.ics", "semantic": _parse_ical_semantic(content), "record": r,
        }
    return expected


def _comparison_key(semantic: dict):
    if semantic.get("kind") == "contact":
        return ("contact", semantic.get("record_id"))
    return (semantic.get("component") or "UNKNOWN", semantic.get("record_id"))


def _clean_semantic(s: dict) -> dict:
    # Ignore bookkeeping fields and serialization-only differences.
    ignored = {"uid", "record_id", "kind", "component"}
    return {k: (v.strip() if isinstance(v, str) else v)
            for k, v in s.items() if k not in ignored and v not in (None, "")}


def contact_record_to_semantic(record: dict) -> dict:
    """Return the normalized contact semantic used by the two-way baseline.

    This intentionally round-trips through the same vCard serializer/parser as
    CardDAV comparison so destructive pre-delete checks compare like with like.
    """
    return _clean_semantic(_parse_vcard_semantic(contact_to_vcard(record)))


def analyze_reverse_dry_run(export: dict, dav_snapshot: dict, state_file: Path | None = None,
                            logger=print) -> dict:
    """Compare Thunderbird/Radicale with IC35. Does NOT write to IC35 or DAV.

    v1.1.1: Thunderbird/CardDAV may store an edited server contact under a
    different resource filename.  X-IC35-RECORD-ID is therefore not the only
    identity signal: the stable vCard/iCalendar UID is used as a second,
    conservative mapping key.  When both the original ic35-* resource and a
    Thunderbird-created alias exist, the alias is preferred for comparison and
    the duplicate is reported as a warning.  No resource is modified here.
    """
    expected = _expected_semantic_from_export(export)
    report = {
        "mode": "dry-run", "created_at": datetime.now(timezone.utc).isoformat(),
        "device": export.get("device", ""),
        "contacts": {"new": [], "update": [], "delete": [], "unchanged": []},
        "calendar": {"new": [], "update": [], "delete": [], "unchanged": []},
        "warnings": [],
        "identity_aliases": [],
    }

    previous_resources = {"addressbook": set(), "calendar": set()}
    state = {}
    bound_resource_ids = {"addressbook": {}, "calendar": {}}
    bound_id_resources = {"addressbook": {}, "calendar": {}}
    if state_file:
        try:
            state = json.loads(Path(state_file).read_text(encoding="utf-8"))
            for k in previous_resources:
                previous_resources[k] = set(state.get("resources", {}).get(k, []))
            ab_bindings = state.get("bindings", {}).get("addressbook", {})
            if isinstance(ab_bindings, dict):
                for rid, binding in ab_bindings.items():
                    try:
                        rid_i = int(rid)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(binding, dict) and binding.get("resource"):
                        resource_name = str(binding["resource"])
                        bound_resource_ids["addressbook"][resource_name] = ("contact", rid_i)
                        bound_id_resources["addressbook"][("contact", rid_i)] = resource_name
            cal_bindings = state.get("bindings", {}).get("calendar", {})
            if isinstance(cal_bindings, dict):
                for key_s, binding in cal_bindings.items():
                    if not isinstance(binding, dict) or not binding.get("resource"):
                        continue
                    component = str(binding.get("component") or str(key_s).split(":", 1)[0]).upper()
                    try:
                        rid_i = int(binding.get("record_id") if binding.get("record_id") is not None else str(key_s).split(":", 1)[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if component not in ("VEVENT", "VTODO"):
                        continue
                    resource_name = str(binding["resource"])
                    bound_resource_ids["calendar"][resource_name] = (component, rid_i)
                    bound_id_resources["calendar"][(component, rid_i)] = resource_name
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}

    for collection_key, report_key in (("addressbook", "contacts"), ("calendar", "calendar")):
        server_by_key = {}
        alias_by_key = {}
        new_without_id = []

        # Build a UID -> IC35 comparison-key map from what this device would
        # publish.  Only unique, non-empty UIDs are accepted.
        uid_candidates = {}
        for exp_key, info in expected[collection_key].items():
            uid = (info.get("semantic", {}).get("uid") or "").strip()
            if uid:
                uid_candidates.setdefault(uid, []).append(exp_key)
        expected_uid = {uid: keys[0] for uid, keys in uid_candidates.items() if len(keys) == 1}

        for filename, item in dav_snapshot.get(collection_key, {}).items():
            sem = item.get("semantic", {})
            key = _comparison_key(sem)
            if key[1] is not None:
                server_by_key[key] = (filename, sem)
                continue

            # First use the bridge's durable Record-ID <-> resource binding.
            # This survives Thunderbird rewriting/removing custom X-IC35 fields.
            bound_key = bound_resource_ids.get(collection_key, {}).get(filename)
            alias_key = None
            if bound_key is not None:
                candidate = bound_key
                if candidate in expected[collection_key]:
                    alias_key = candidate
                    report["identity_aliases"].append({
                        "collection": collection_key, "resource": filename,
                        "component": candidate[0], "record_id": candidate[1],
                        "uid": sem.get("uid", ""),
                        "reason": "persistent bridge binding",
                    })
                    logger(f"Dry-Run State-Zuordnung: {filename} -> {candidate[0]} IC35 Record-ID {candidate[1]}")

            uid = (sem.get("uid") or "").strip()
            if alias_key is None:
                alias_key = expected_uid.get(uid)
            if alias_key is not None:
                alias_by_key.setdefault(alias_key, []).append((filename, sem))
                if bound_key is None:
                    report["identity_aliases"].append({
                        "collection": collection_key,
                        "resource": filename,
                        "record_id": alias_key[1],
                        "uid": uid,
                        "reason": "UID matches an IC35-published item",
                    })
                    logger(
                        f"Dry-Run UID-Zuordnung: {filename} -> "
                        f"IC35 Record-ID {alias_key[1]} (UID={uid})"
                    )
            else:
                new_without_id.append((filename, sem))

        # Truly new Thunderbird items: no IC35 record id and no known UID.
        for filename, sem in new_without_id:
            summary = sem.get("summary") or " ".join(
                x for x in (sem.get("first"), sem.get("last")) if x
            ).strip() or sem.get("uid") or filename
            report[report_key]["new"].append({
                "resource": filename,
                "uid": sem.get("uid", ""),
                "summary": summary,
                "semantic": _clean_semantic(sem),
            })
            logger(
                f"Dry-Run NEU ohne IC35-ID: {filename}; "
                f"UID={sem.get('uid','')!r}; Eintrag={summary!r}"
            )

        for key, info in expected[collection_key].items():
            direct = server_by_key.get(key)
            aliases = alias_by_key.get(key, [])

            # A UID alias normally means Thunderbird has materialized the same
            # logical contact/item under its own href.  Prefer the single alias
            # for semantic comparison.  Multiple aliases are intentionally NOT
            # guessed: report them and keep the direct resource if available.
            chosen = direct
            if len(aliases) == 1:
                alias_filename, alias_sem = aliases[0]
                chosen = (alias_filename, alias_sem)
                if direct is not None and direct[0] != alias_filename:
                    warning = (
                        f"Doppelte CardDAV/CalDAV-Ressource fuer IC35 Record-ID {key[1]}: "
                        f"{direct[0]} und {alias_filename}. Fuer den Dry-Run wird die "
                        f"Thunderbird-Ressource {alias_filename} anhand derselben UID verwendet."
                    )
                    report["warnings"].append(warning)
                    logger("WARNUNG: " + warning)
            elif len(aliases) > 1:
                warning = (
                    f"Mehrere UID-Aliase fuer IC35 Record-ID {key[1]} gefunden: "
                    + ", ".join(x[0] for x in aliases)
                    + ". Keine automatische Alias-Auswahl."
                )
                report["warnings"].append(warning)
                logger("WARNUNG: " + warning)

            if chosen is not None:
                filename, server_sem = chosen
                if _clean_semantic(server_sem) == _clean_semantic(info["semantic"]):
                    report[report_key]["unchanged"].append({
                        "record_id": key[1], "resource": filename,
                        "uid": server_sem.get("uid", ""),
                    })
                else:
                    report[report_key]["update"].append({
                        "record_id": key[1], "resource": filename,
                        "uid": server_sem.get("uid", ""),
                        "ic35": _clean_semantic(info["semantic"]),
                        "thunderbird": _clean_semantic(server_sem),
                    })
            else:
                # Only call it a deletion when the previous IC35->TB sync state says
                # this exact resource had been published before. Otherwise absence
                # might simply mean Thunderbird wasn't fully initialized.
                filename = info["filename"]
                # Contacts may have a Thunderbird UUID href rather than the
                # canonical ic35-address-*.vcf filename.  For deletion
                # detection the durable Record-ID -> href binding is the
                # authoritative previous resource name.
                filename = bound_id_resources[collection_key].get(key, filename)
                if filename in previous_resources[collection_key]:
                    report[report_key]["delete"].append({
                        "record_id": key[1], "resource": filename,
                    })
                else:
                    report["warnings"].append(
                        f"{filename} fehlt in Thunderbird, war aber nicht im bekannten Sync-State; keine Löschung angenommen."
                    )

        # A resource carrying an explicit IC35 ID that no longer exists on the
        # device is a candidate for a new device record (the old ID cannot be
        # preserved by write-new).
        for key, (filename, sem) in server_by_key.items():
            if key not in expected[collection_key]:
                report[report_key]["new"].append({
                    "resource": filename, "former_record_id": key[1],
                    "uid": sem.get("uid", ""),
                    "summary": sem.get("summary") or " ".join(
                        x for x in (sem.get("first"), sem.get("last")) if x
                    ).strip() or filename,
                    "semantic": _clean_semantic(sem),
                })

    for kind in ("contacts", "calendar"):
        c = report[kind]
        logger(
            f"Dry-Run {kind}: neu={len(c['new'])}, geändert={len(c['update'])}, "
            f"gelöscht={len(c['delete'])}, unverändert={len(c['unchanged'])}"
        )
        for item in c["update"]:
            logger(
                f"  UPDATE Record-ID {item.get('record_id')}: "
                f"Ressource={item.get('resource')} UID={item.get('uid','')}"
            )
            ic35_sem = item.get("ic35", {})
            tb_sem = item.get("thunderbird", {})
            keys = sorted(set(ic35_sem) | set(tb_sem))
            for field in keys:
                a, b = ic35_sem.get(field, ""), tb_sem.get(field, "")
                if a != b:
                    logger(f"    {field}: IC35={a!r} -> Thunderbird={b!r}")
    logger("Dry-Run: Es wurden KEINE Daten auf dem IC35 geschrieben.")
    return report


# ---------------------------------------------------------------------------
# Local Radicale storage helpers (Windows-stable mode, v1.1.0)
# ---------------------------------------------------------------------------

def cleanup_radicale_storage(storage_root: Path, logger=print) -> dict:
    """Remove only Radicale cache/temp artifacts that upstream documents as safe.

    User data (.vcf/.ics) is never deleted here. Collection metadata is only
    repaired when missing or unreadable JSON.
    """
    import shutil
    storage_root = Path(storage_root)
    paths = ensure_radicale_storage(storage_root)
    root = storage_root / "collection-root"
    removed_cache = 0
    removed_temp = 0

    if root.exists():
        for path in list(root.rglob('.Radicale.cache')):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed_cache += 1
        for path in list(root.rglob('.Radicale.tmp-*')):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                removed_temp += 1
            except OSError:
                pass

    # Keep metadata deliberately simple and documented.
    expected = {
        paths['calendar'] / '.Radicale.props': {
            'tag': 'VCALENDAR',
            'D:displayname': 'Siemens IC35',
            'C:supported-calendar-component-set': 'VEVENT,VTODO',
        },
        paths['addressbook'] / '.Radicale.props': {
            'tag': 'VADDRESSBOOK',
            'D:displayname': 'Siemens IC35 Kontakte',
        },
    }
    repaired_props = 0
    for props_path, fallback in expected.items():
        rewrite = False
        try:
            obj = json.loads(props_path.read_text(encoding='utf-8'))
            if not isinstance(obj, dict) or obj.get('tag') != fallback['tag']:
                rewrite = True
        except Exception:
            rewrite = True
        if rewrite:
            props_path.write_text(json.dumps(fallback, ensure_ascii=False), encoding='utf-8')
            repaired_props += 1

    logger(
        f"Radicale-Speicher geprüft: Cache={removed_cache} entfernt, "
        f"Temp={removed_temp} entfernt, Props={repaired_props} repariert."
    )
    return {'cache_removed': removed_cache, 'temp_removed': removed_temp,
            'props_repaired': repaired_props}


def fetch_storage_snapshot(storage_root: Path, logger=print) -> dict:
    """Read Thunderbird/Radicale collections directly while server is stopped.

    Safety rule: a partial snapshot must NEVER be interpreted as Thunderbird
    deletions.  If even one matching resource cannot be read, abort the entire
    dry-run with a diagnostic instead of returning an incomplete result.
    """
    paths = ensure_radicale_storage(storage_root)
    result = {'addressbook': {}, 'calendar': {}}
    failures = []

    def describe_path(path: Path) -> str:
        parts = []
        try:
            parts.append(f"is_file={path.is_file()}")
            parts.append(f"is_dir={path.is_dir()}")
            parts.append(f"is_symlink={path.is_symlink()}")
        except OSError as exc:
            parts.append(f"type-check={exc}")
        try:
            st = path.lstat()
            parts.append(f"mode=0x{st.st_mode:X}")
            parts.append(f"size={st.st_size}")
        except OSError as exc:
            parts.append(f"lstat={exc}")
        if path.is_symlink():
            try:
                parts.append(f"target={os.readlink(path)!r}")
            except OSError as exc:
                parts.append(f"readlink={exc}")
        if path.is_dir():
            try:
                children = [p.name for p in list(path.iterdir())[:8]]
                parts.append(f"children={children!r}")
            except OSError as exc:
                parts.append(f"listdir={exc}")
        return ', '.join(parts)

    for key, folder, patterns in (
        ('addressbook', paths['addressbook'], ('*.vcf',)),
        ('calendar', paths['calendar'], ('*.ics',)),
    ):
        files = []
        for pat in patterns:
            files.extend(folder.glob(pat))
        for path in sorted(files):
            if path.name.startswith('.'):
                continue
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
                result[key][path.name] = {
                    'etag': '',
                    'content': text,
                    'semantic': _semantic_content(text, path.name),
                }
                logger(f"Storage READ {path.name}")
            except OSError as exc:
                detail = describe_path(path)
                logger(f"FEHLER: {path.name} konnte nicht gelesen werden: {exc}")
                logger(f"        Pfad-Diagnose: {detail}")
                failures.append((key, path.name, str(exc), detail))
        logger(f"Storage-Snapshot {key}: {len(result[key])} Ressourcen gefunden.")

    if failures:
        names = ', '.join(name for _, name, _, _ in failures)
        raise RuntimeError(
            "Radicale-Snapshot ist unvollständig; der Dry-Run wurde aus Sicherheitsgründen abgebrochen. "
            f"Nicht lesbar: {names}. Bitte 'Radicale-Speicher reparieren' verwenden."
        )
    return result


def publish_export_storage(export: dict, storage_root: Path, export_dir: Path,
                           state_file: Path, logger=print) -> dict:
    """Publish IC35 items directly while Radicale is stopped.

    Non-IC35 resources are preserved. Only ic35-* resources are created,
    overwritten or removed. Cache/temp artifacts are deleted afterwards so
    Radicale rebuilds ETags/sync metadata on restart.
    """
    paths = ensure_radicale_storage(storage_root)
    calendar = paths['calendar']
    addressbook = paths['addressbook']
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    addresses = export.get('databases', {}).get('Addresses', {}).get('records', [])
    schedules = export.get('databases', {}).get('Schedule', {}).get('records', [])
    todos = export.get('databases', {}).get('To Do List', {}).get('records', [])
    memos = export.get('databases', {}).get('Memo', {}).get('records', [])

    wanted_ab = set()
    wanted_cal = set()

    # Persistent record-id <-> CardDAV resource bindings.  These are crucial
    # when Thunderbird deletes a contact locally: the IC35 is authoritative in
    # the forward-sync direction, so a still-existing IC35 record must be able
    # to resurrect the same CardDAV href/UID on the next publish.
    persisted_bindings = {}
    try:
        old_state = json.loads(Path(state_file).read_text(encoding='utf-8')) if Path(state_file).exists() else {}
    except (json.JSONDecodeError, OSError):
        old_state = {}

    for rid_text, binding in old_state.get('bindings', {}).get('addressbook', {}).items():
        try:
            persisted_bindings[int(rid_text)] = {
                'resource': str(binding.get('resource') or ''),
                'uid': str(binding.get('uid') or ''),
            }
        except (TypeError, ValueError, AttributeError):
            pass

    # Migration from v1.1.0, which stored only the most recent reverse-write.
    legacy = old_state.get('last_reverse_write', {})
    try:
        legacy_rid = int(legacy.get('record_id'))
        legacy_resource = str(legacy.get('resource') or '')
        if legacy_resource:
            persisted_bindings.setdefault(legacy_rid, {
                'resource': legacy_resource,
                'uid': str(legacy.get('uid') or ''),
            })
    except (TypeError, ValueError):
        pass

    active_bindings = {}

    # Reuse Thunderbird-created CardDAV hrefs once they carry an explicit
    # X-IC35-RECORD-ID. This prevents a reverse-synced contact from later
    # being duplicated as ic35-address-XXXXXX.vcf. Preserve its stable UID.
    mapped_address_resources = {}
    for path in addressbook.glob('*.vcf'):
        try:
            sem = _parse_vcard_semantic(path.read_text(encoding='utf-8', errors='replace'))
            rid = sem.get('record_id')
            if rid is not None:
                mapped_address_resources.setdefault(int(rid), []).append((path.name, sem.get('uid', '')))
        except Exception as exc:
            logger(f"WARNUNG: Mapping-Scan {path.name} fehlgeschlagen: {exc}")

    for record in addresses:
        if record.get('error') or record.get('deleted'):
            continue
        rid = int(record.get('record_id', 0) or 0)
        candidates = mapped_address_resources.get(rid, [])
        if len(candidates) == 1:
            filename, uid = candidates[0]
        elif len(candidates) == 0 and rid in persisted_bindings and persisted_bindings[rid].get('resource'):
            # The mapped Thunderbird resource vanished (e.g. user deleted it),
            # but the IC35 record still exists. Recreate the exact same href and
            # stable UID so CardDAV sees the server-side item again.
            filename = persisted_bindings[rid]['resource']
            uid = persisted_bindings[rid].get('uid') or None
            logger(
                f"Storage RESTORE {filename}: IC35 Record-ID {rid} ist noch vorhanden; "
                "gelöschte CardDAV-Ressource wird wiederhergestellt."
            )
        else:
            filename = f"ic35-address-{rid:06d}.vcf"
            uid = None
            if len(candidates) > 1:
                logger(f"WARNUNG: Mehrere CardDAV-Ressourcen mit IC35 Record-ID {rid}; verwende {filename}.")
        wanted_ab.add(filename)
        rendered = contact_to_vcard(record, uid_override=uid or None)
        _atomic_write(addressbook / filename, rendered)
        # Persist the actual UID we published.
        try:
            published_sem = _parse_vcard_semantic(rendered)
            published_uid = str(published_sem.get('uid') or '')
        except Exception:
            published_uid = str(uid or '')
        active_bindings[str(rid)] = {'resource': filename, 'uid': published_uid}
        logger(f"Storage WRITE {filename}")

    for record in schedules:
        if record.get('error') or record.get('deleted'):
            continue
        content = event_to_ics(record)
        if not content:
            logger(f"WARNUNG: Termin Record {record.get('record_id')} konnte nicht konvertiert werden.")
            continue
        filename = f"ic35-schedule-{record.get('record_id', 0):06d}.ics"
        wanted_cal.add(filename)
        _atomic_write(calendar / filename, content)
        logger(f"Storage WRITE {filename}")

    for record in todos:
        if record.get('error') or record.get('deleted'):
            continue
        filename = f"ic35-todo-{record.get('record_id', 0):06d}.ics"
        wanted_cal.add(filename)
        _atomic_write(calendar / filename, todo_to_ics(record))
        logger(f"Storage WRITE {filename}")

    for path in calendar.glob('ic35-*.ics'):
        if path.name not in wanted_cal:
            path.unlink(missing_ok=True)
            logger(f"Storage DELETE {path.name}")
    for path in addressbook.glob('ic35-*.vcf'):
        if path.name not in wanted_ab:
            path.unlink(missing_ok=True)
            logger(f"Storage DELETE {path.name}")

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    json_path = export_dir / f"IC35_Organizer_Export_{stamp}.json"
    json_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding='utf-8')

    memo_path = export_dir / f"IC35_Memos_{stamp}.txt"
    memo_lines = []
    for record in memos:
        if record.get('error') or record.get('deleted'):
            continue
        memo_lines += [
            f"Betreff: {_field(record, 'Betreff', '')}",
            f"Kategorie: {_field(record, 'category', '')}",
            f"IC35 Record-ID: {record.get('record_id', '')}", '',
            str(_field(record, 'Notizen', '')), '', '-' * 60, '',
        ]
    memo_path.write_text('\n'.join(memo_lines), encoding='utf-8')

    cleanup_radicale_storage(storage_root, logger)

    state = {
        'version': '1.5.0',
        'synced_at': datetime.now(timezone.utc).isoformat(),
        'device': export.get('device', ''),
        'resources': {
            'addressbook': sorted(wanted_ab),
            'calendar': sorted(wanted_cal),
        },
        'bindings': {
            'addressbook': active_bindings,
        },
        'counts': {
            'addresses': len(wanted_ab),
            'schedule_and_todos': len(wanted_cal),
            'memos': len([r for r in memos if not r.get('error') and not r.get('deleted')]),
        },
    }
    state_file = Path(state_file)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

    return {
        'contacts': len(wanted_ab),
        'calendar_items': len(wanted_cal),
        'memos': state['counts']['memos'],
        'json_export': str(json_path),
        'memo_export': str(memo_path),
        'state_file': str(state_file),
    }
