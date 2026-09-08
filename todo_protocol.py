"""Aufgabe transport, using ic35link 1.18 syntrans.c and doc/ic35sync.txt.

Separate module deliberately leaves the proven address/calendar transport intact.
"""
import ic35_protocol as p


def encode(fields):
    parts = []
    for name, limit in (("Start", 8), ("Ende", 8), ("Erledigt", 1),
                        ("Prioritaet", 1), ("Betreff", 60), ("Notizen", 255),
                        ("category-id", 1), ("category", 8)):
        if name in ("category-id", "Erledigt", "Prioritaet"):
            value = fields.get(name)
            raw = b"" if value is None else bytes([int(value)])
        else:
            value = fields.get(name, "") or ""
            if "\x00" in value:
                raise ValueError(f"Aufgabe {name}: Nullzeichen sind nicht erlaubt.")
            try:
                raw = value.encode("cp1252", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError(f"Aufgabe {name}: Zeichen ist auf IC35 nicht darstellbar.") from exc
            if len(raw) > limit:
                raise ValueError(f"Aufgabe {name}: {len(raw)} Byte, maximal {limit}. Keine Kürzung.")
        parts.append(raw)
    return bytes(map(len, parts)) + b"".join(parts)


def values(rec):
    if not rec or rec.get("error") or rec.get("deleted") or rec.get("file_id") != 7:
        raise ValueError("Ungültiger Aufgabe-Datensatz.")
    fields = rec.get("fields", {})
    if set(fields) != {"Start", "Ende", "Erledigt", "Prioritaet", "Betreff", "Notizen", "category-id", "category"}:
        raise ValueError("Unvollständiger Aufgabe-Datensatz; Abgleich gestoppt.")
    return {key: item["value"] for key, item in fields.items()}


def matches_written(actual, expected):
    expected = dict(expected)
    # Real IC35 firmware fills an empty Start with Ende when creating a task.
    if not expected.get("Start") and actual.get("Start") == expected.get("Ende"):
        expected["Start"] = actual["Start"]
    return actual == expected


def write(ser, fd, fields, rid=None):
    data = encode(fields)
    if rid is not None and not 0 < int(rid) <= 0xFFFFFF:
        raise ValueError("Ungültige Aufgabe-ID.")
    uid = int(rid).to_bytes(3, "little") + b"\x07" if rid else bytes(4)
    result = None
    for offset in range(0, len(data), 80):
        chunk = data[offset:offset + 80]
        last = offset + len(chunk) == len(data)
        if offset == 0:
            chunk = (b"\x01" + (b"\x09" if rid else b"\x08") + fd + uid
                     + b"\x10\x00\x00\x00\x00" + len(data).to_bytes(4, "little") + chunk)
        label = f"Aufgabe {'UPDATE' if rid else 'CREATE'} Block {offset // 80 + 1}"
        request = p.make_fragment_command(0x82 if last else 2, 0x49 if last else 0x48, chunk)
        response = p.l1_transaction(ser, request, label)
        if response is None:
            raise RuntimeError(f"{label}: keine Antwort.")
        result = p._decode_write_response(response, last, label)
        if result is None:
            raise RuntimeError(f"{label}: nicht bestätigt.")
    new_id, file_id = result
    if file_id != 7 or new_id <= 0 or (rid and new_id != rid):
        raise RuntimeError("Unerwartete Aufgabe-Schreibantwort.")
    raw = p.read_record_raw_by_id(ser, "To Do List", fd, new_id)
    rec = p.decode_record("To Do List", raw, -1) if raw else None
    expected = dict(fields)
    if not matches_written(values(rec), expected):
        raise RuntimeError("Aufgabe-Rücklesen stimmt nicht mit Schreibdaten überein.")
    return rec


def read_all(ser, fd):
    count = p.read_database_count(ser, "To Do List", fd)
    if count is None:
        raise RuntimeError("Aufgabe-Anzahl nicht lesbar.")
    records = []
    for index in range(count):
        raw = p.read_record_raw_by_index(ser, "To Do List", fd, index)
        rec = p.decode_record("To Do List", raw, index) if raw else None
        if not rec:
            raise RuntimeError("Aufgabe nicht vollständig lesbar.")
        if not rec.get("deleted"):
            values(rec)
        records.append(rec)
    return records


def delete(ser, fd, rid):
    if not 0 < int(rid) <= 0xFFFFFF:
        raise ValueError("Ungültige Aufgabe-ID.")
    response = p.command_transaction(ser, 0x83,
        b"\x01\x02" + fd + int(rid).to_bytes(3, "little") + b"\x07", f"Delete Aufgabe {rid}")
    if response is None:
        raise RuntimeError("Aufgabe-Löschung nicht bestätigt.")
    if any(r["record_id"] == rid and not r.get("deleted") for r in read_all(ser, fd)):
        raise RuntimeError("Aufgabe nach Löschung noch vorhanden.")
