import sys
import time
import getpass
import json
from datetime import datetime
from pathlib import Path

try:
    import serial
except ImportError:
    print("FEHLER: pyserial ist nicht installiert.")
    print("Installiere es mit:")
    print("  py -m pip install pyserial")
    input("\nEnter zum Beenden ...")
    raise SystemExit(1)

PORT = "COM5"
BAUDRATE = 115200

WELCOME_CHAR = b"A"
WELCOME_TEXT = b"WELCOME"

L1_INIT = bytes.fromhex("01 03 00")
L1_DATAREQ = bytes.fromhex("04 03 00")
L1_EXIT = bytes.fromhex("05 03 00")

IDENTIFY_L2 = (
    bytes.fromhex("80 26 00 10 00 64 00 4A 1F 00")
    + b"INVENTEC CORPORATION PRODUCT"
)

DATABASES = [
    "Addresses",
    "Memo",
    "Schedule",
    "To Do List",
]

FIELD_SPECS = {
    "Addresses": [
        ("Vorname", "text"),
        ("Nachname", "text"),
        ("Firma", "text"),
        ("Tel.Privat", "text"),
        ("Tel.Buero", "text"),
        ("Handy", "text"),
        ("Fax", "text"),
        ("Strasse", "text"),
        ("Ort", "text"),
        ("PLZ", "text"),
        ("Bundesland", "text"),
        ("Land", "text"),
        ("E-Mail1", "text"),
        ("E-Mail2", "text"),
        ("URL", "text"),
        ("Geburtstag", "text"),
        ("Notizen", "text"),
        ("category-id", "bin"),
        ("(def.)1", "text"),
        ("(def.)2", "text"),
        ("category", "text"),
    ],
    "Memo": [
        ("Betreff", "text"),
        ("Notizen", "text"),
        ("category-id", "bin"),
        ("category", "text"),
    ],
    "Schedule": [
        ("Betreff", "text"),
        ("Start(Datum)", "text"),
        ("Start(Zeit)", "text"),
        ("Ende(Zeit)", "text"),
        ("AlrmBef", "bin"),
        ("Notizen", "text"),
        ("AlrmRep", "bin"),
        ("Ende(Datum)", "text"),
        ("EndRepeat", "text"),
        ("RepAlln", "bin"),
    ],
    "To Do List": [
        ("Start", "text"),
        ("Ende", "text"),
        ("Erledigt", "bin"),
        ("Prioritaet", "bin"),
        ("Betreff", "text"),
        ("Notizen", "text"),
        ("category-id", "bin"),
        ("category", "text"),
    ],
}

FILE_IDS = {
    "Addresses": 0x05,
    "Memo": 0x06,
    "To Do List": 0x07,
    "Schedule": 0x08,
}

EXPORTFILE = Path(__file__).with_name(
    f"IC35_Organizer_Export_{datetime.now():%Y%m%d_%H%M%S}.json"
)

LOGFILE = Path(__file__).with_name(
    f"IC35_Sync_v0.7_{datetime.now():%Y%m%d_%H%M%S}.log"
)


# ---------------------------------------------------------------------------
# Logging / serial helpers
# ---------------------------------------------------------------------------

def hx(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def log(msg=""):
    print(msg)
    with LOGFILE.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def tx(ser, data: bytes, label: str, paced: bool = False, sensitive: bool = False):
    if sensitive:
        log(f"TX {label}: <aus Sicherheitsgruenden nicht protokolliert>")
    else:
        log(f"TX {label}: {hx(data)}")

    if not paced or len(data) <= 29:
        ser.write(data)
        ser.flush()
        return

    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + 29]
        ser.write(chunk)
        ser.flush()
        offset += len(chunk)

        if offset < len(data):
            log(f"   TX-Pause nach {offset} Bytes: 10 ms")
            time.sleep(0.010)


def read_exact(ser, count: int, timeout: float) -> bytes:
    end = time.monotonic() + timeout
    out = bytearray()

    while len(out) < count and time.monotonic() < end:
        chunk = ser.read(count - len(out))
        if chunk:
            out.extend(chunk)
        else:
            time.sleep(0.005)

    return bytes(out)


def read_some(ser, duration: float) -> bytes:
    end = time.monotonic() + duration
    out = bytearray()

    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            out.extend(ser.read(n))
        else:
            b = ser.read(1)
            if b:
                out.extend(b)
            else:
                time.sleep(0.005)

    return bytes(out)


def wait_for_byte(ser, expected: int, timeout: float, label: str):
    end = time.monotonic() + timeout
    seen = bytearray()

    while time.monotonic() < end:
        b = ser.read(1)
        if not b:
            continue

        seen.extend(b)

        if b[0] == expected:
            log(f"RX {label}: {hx(bytes(seen))}")
            return True, bytes(seen)

        # F3 wurde von unserem IC35 bei einem abgelehnten L1-Frame beobachtet.
        if b[0] == 0xF3:
            log(f"RX {label}: {hx(bytes(seen))}  (F3 / Frame abgelehnt)")
            return False, bytes(seen)

    if seen:
        log(f"RX {label} (unerwartet): {hx(bytes(seen))}")
    else:
        log(f"RX {label}: <Timeout>")

    return False, bytes(seen)


# ---------------------------------------------------------------------------
# Level 1
# ---------------------------------------------------------------------------

def make_l1_datasel(l2_data: bytes) -> bytes:
    # Level-1:
    # 02 ll ll <L2data> cc cc
    # ll umfasst die komplette PDU inklusive der zwei Checksum-Bytes.
    pdu_len = 3 + len(l2_data) + 2
    body = bytes([0x02]) + pdu_len.to_bytes(2, "little") + l2_data
    checksum = (sum(body) & 0xFFFF).to_bytes(2, "little")
    return body + checksum


def read_l1_datarsp(ser, timeout: float = 3.0):
    # F2 ll ll <L2data> cc cc
    ok, _ = wait_for_byte(ser, 0xF2, timeout, "L1 datarsp start")
    if not ok:
        return None

    length_bytes = read_exact(ser, 2, 1.0)
    if len(length_bytes) != 2:
        log("FEHLER: L1-Laengenfeld unvollstaendig.")
        return None

    pdu_len = int.from_bytes(length_bytes, "little")
    if pdu_len < 5 or pdu_len > 8192:
        log(f"FEHLER: Unplausible L1-Laenge: {pdu_len}")
        return None

    rest = read_exact(ser, pdu_len - 3, 4.0)
    if len(rest) != pdu_len - 3:
        log(
            f"FEHLER: L1-Antwort unvollstaendig: "
            f"{len(rest)}/{pdu_len - 3} Bytes nach Header"
        )
        if rest:
            log(f"RX Rest: {hx(rest)}")
        return None

    frame = bytes([0xF2]) + length_bytes + rest
    log(f"RX L1 datarsp komplett: {hx(frame)}")

    body = frame[:-2]
    rx_checksum = int.from_bytes(frame[-2:], "little")
    calc_checksum = sum(body) & 0xFFFF

    log(
        f"L1 Checksumme: empfangen=0x{rx_checksum:04X}, "
        f"berechnet=0x{calc_checksum:04X}"
    )

    if rx_checksum != calc_checksum:
        log("FEHLER: L1-Pruefsumme stimmt nicht.")
        return None

    log("L1-Pruefsumme: OK")
    return frame[3:-2]


def l1_transaction(ser, l2_data: bytes, label: str, sensitive: bool = False):
    """
    Eine komplette Level-1-Transaktion gemaess ic35sync.txt:

        01 03 00       -> F0
        02 ... L2 ...  -> F1
        04 03 00       -> F2 ... L2 response ...
        05 03 00       -> F1

    Wichtig: Dieser komplette Zyklus wird FUER JEDEN L2-Befehl neu
    ausgefuehrt. v0.5 hatte Level-1 nur einmal vor Identify gestartet;
    unser IC35 beantwortete den anschliessenden Power-datasel deshalb
    mit F3.
    """
    log()
    log(f"--- L1-Transaktion: {label} ---")

    # 1) init
    tx(ser, L1_INIT, f"L1 init / {label}")
    ok, _ = wait_for_byte(ser, 0xF0, 2.0, f"L1 ack0 / {label}")
    if not ok:
        log(f"FEHLER: Kein F0 beim Start von {label}.")
        return None

    # 2) select L2 data
    frame = make_l1_datasel(l2_data)
    tx(
        ser,
        frame,
        f"L1 datasel / {label}",
        paced=True,
        sensitive=sensitive,
    )

    ok, _ = wait_for_byte(ser, 0xF1, 2.5, f"L1 ack1 / {label}")
    if not ok:
        log(f"FEHLER: Keine F1-Bestaetigung fuer {label}.")
        # best effort: Level-1-Zyklus beenden
        try:
            tx(ser, L1_EXIT, f"L1 exit nach Fehler / {label}")
            wait_for_byte(ser, 0xF1, 1.0, f"L1 exit ack1 / {label}")
        except Exception:
            pass
        return None

    # 3) request response
    tx(ser, L1_DATAREQ, f"L1 datareq / {label}")
    response = read_l1_datarsp(ser, 4.0)

    # 4) komplette Level-1-Transaktion immer mit exit beenden
    tx(ser, L1_EXIT, f"L1 exit / {label}")
    exit_ok, _ = wait_for_byte(ser, 0xF1, 2.0, f"L1 exit ack1 / {label}")

    if not exit_ok:
        log(f"WARNUNG: Kein F1 beim Beenden von {label}.")

    if response is None:
        log(f"FEHLER: Keine gueltige Antwort fuer {label}.")
        return None

    return response


# ---------------------------------------------------------------------------
# Level 2 / Level 3
# ---------------------------------------------------------------------------

def make_l3(l3_id: int, l4_data: bytes) -> bytes:
    total_len = 3 + len(l4_data)
    return bytes([l3_id]) + total_len.to_bytes(2, "little") + l4_data


def make_l2(l2_id: int, l3_data: bytes) -> bytes:
    total_len = 3 + len(l3_data)
    return bytes([l2_id]) + total_len.to_bytes(2, "little") + l3_data


def make_command(l2_id: int, l4_data: bytes) -> bytes:
    # Normale Kommandos benutzen L3-ID 49.
    return make_l2(l2_id, make_l3(0x49, l4_data))


def parse_l2_response(l2: bytes, label: str):
    if len(l2) < 3:
        log(f"FEHLER {label}: L2-Antwort zu kurz.")
        return None

    l2_id = l2[0]
    l2_len = int.from_bytes(l2[1:3], "little")

    log(f"{label}: L2-ID=0x{l2_id:02X}, Laenge={l2_len}")

    if l2_len != len(l2):
        log(
            f"WARNUNG {label}: L2-Laengenfeld={l2_len}, "
            f"tatsaechlich={len(l2)}"
        )

    # A0 03 00 = response done, keine L3-Daten.
    if len(l2) == 3:
        return b""

    if len(l2) < 6:
        log(f"FEHLER {label}: L3-Header fehlt.")
        return None

    l3_id = l2[3]
    l3_len = int.from_bytes(l2[4:6], "little")

    log(f"{label}: L3-ID=0x{l3_id:02X}, Laenge={l3_len}")

    if l3_id not in (0x48, 0x49, 0x4A):
        log(f"WARNUNG {label}: Unerwartete L3-ID 0x{l3_id:02X}")

    if 3 + l3_len > len(l2):
        log(f"FEHLER {label}: L3-Laenge groesser als vorhandene Daten.")
        return None

    return l2[6:3 + l3_len]


def command_transaction(
    ser,
    l2_id: int,
    l4_data: bytes,
    label: str,
    sensitive: bool = False,
):
    request = make_command(l2_id, l4_data)
    response = l1_transaction(ser, request, label, sensitive=sensitive)

    if response is None:
        return None

    return parse_l2_response(response, label)



# ---------------------------------------------------------------------------
# Controlled write support (v1.1.0)
# ---------------------------------------------------------------------------

ADDRESS_MAX_BYTES = {
    "Vorname": 50, "Nachname": 50, "Firma": 128,
    "Tel.Privat": 48, "Tel.Buero": 48, "Handy": 48, "Fax": 48,
    "Strasse": 128, "Ort": 60, "PLZ": 10, "Bundesland": 40,
    "Land": 15, "E-Mail1": 80, "E-Mail2": 80, "URL": 128,
    "Geburtstag": 10, "Notizen": 255, "(def.)1": 128,
    "(def.)2": 128, "category": 8,
}


def encode_address_record(fields: dict, logger=None) -> bytes:
    """Build IC35 Addresses record data: 21 length bytes followed by field data.

    The record-id/file-id/change-flag header is NOT part of write data.  Text
    uses cp1252, matching the real DCS15 data we have read.
    """
    if logger is None:
        logger = log
    lengths = []
    payloads = []
    for name, ftype in FIELD_SPECS["Addresses"]:
        if ftype == "bin":
            value = fields.get(name, 0)
            try:
                ivalue = int(value) & 0xFF
            except (TypeError, ValueError):
                ivalue = 0
            raw = bytes([ivalue])
        else:
            value = str(fields.get(name, "") or "")
            raw = value.encode("cp1252", errors="replace")
            max_len = ADDRESS_MAX_BYTES.get(name, 255)
            if len(raw) > max_len:
                logger(
                    f"WARNUNG: Addresses-Feld {name} hat {len(raw)} Byte; "
                    f"wird auf {max_len} Byte gekürzt."
                )
                raw = raw[:max_len]
        if len(raw) > 255:
            raw = raw[:255]
        lengths.append(len(raw))
        payloads.append(raw)
    data = bytes(lengths) + b"".join(payloads)
    logger(
        f"Addresses-Schreibdatensatz vorbereitet: {len(data)} Byte "
        f"({len(lengths)} Feldlängen + {len(data)-len(lengths)} Felddaten)."
    )
    return data



SCHEDULE_MAX_BYTES = {
    "Betreff": 60,
    "Start(Datum)": 8,
    "Start(Zeit)": 6,
    "Ende(Zeit)": 6,
    "Notizen": 255,
    "Ende(Datum)": 8,
    "EndRepeat": 8,
}


def encode_schedule_record(fields: dict, logger=None) -> bytes:
    """Build IC35 Schedule record data: 10 length bytes + field payload.

    Binary fields are AlrmBef, AlrmRep and RepAlln. Text is cp1252, exactly
    like the historic ic35link record builder and the real DCS15 data.
    """
    if logger is None:
        logger = log
    lengths = []
    payloads = []
    for name, ftype in FIELD_SPECS["Schedule"]:
        if ftype == "bin":
            try:
                ivalue = int(fields.get(name, 0)) & 0xFF
            except (TypeError, ValueError):
                ivalue = 0
            raw = bytes([ivalue])
        else:
            value = str(fields.get(name, "") or "")
            raw = value.encode("cp1252", errors="replace")
            max_len = SCHEDULE_MAX_BYTES.get(name, 255)
            if len(raw) > max_len:
                logger(
                    f"WARNUNG: Schedule-Feld {name} hat {len(raw)} Byte; "
                    f"wird auf {max_len} Byte gekürzt."
                )
                raw = raw[:max_len]
        if len(raw) > 255:
            raw = raw[:255]
        lengths.append(len(raw))
        payloads.append(raw)
    data = bytes(lengths) + b"".join(payloads)
    logger(
        f"Schedule-Schreibdatensatz vorbereitet: {len(data)} Byte "
        f"({len(lengths)} Feldlängen + {len(data)-len(lengths)} Felddaten)."
    )
    return data


def write_new_schedule_record(ser, fd: bytes, fields: dict) -> tuple[int, int]:
    """Write exactly one new Schedule record using ic35link's 10 00 00 magic."""
    data = encode_schedule_record(fields, logger=log)
    total = len(data)
    offset = 0
    chunk_no = 0
    final_result = None
    while offset < total:
        chunk_no += 1
        chunk = data[offset:offset + 80]
        last = offset + len(chunk) >= total
        if offset == 0:
            # ic35link 1.18 syntrans.c: FILESCHED uses magic 10 00 00.
            l4 = (
                b"\x01\x08" + fd + b"\x00\x00\x00\x00" +
                b"\x10\x00\x00" + b"\x00\x00" +
                total.to_bytes(4, "little") + chunk
            )
        else:
            l4 = chunk
        l2_id = 0x82 if last else 0x02
        l3_id = 0x49 if last else 0x48
        request = make_fragment_command(l2_id, l3_id, l4)
        label = f"Write Schedule Block {chunk_no} ({'last' if last else 'more'})"
        log(
            f"{label}: Record-Bytes {offset}..{offset+len(chunk)-1} "
            f"von {total}, L2=0x{l2_id:02X}, L3=0x{l3_id:02X}"
        )
        response = l1_transaction(ser, request, label)
        if response is None:
            raise RuntimeError(f"Keine gültige Antwort auf {label}.")
        decoded = _decode_write_response(response, last, label)
        if decoded is None:
            raise RuntimeError(f"IC35 hat {label} nicht bestätigt.")
        if last:
            final_result = decoded
        offset += len(chunk)
    if final_result is None:
        raise RuntimeError("IC35 Schedule-Schreibvorgang endete ohne neue Record-ID.")
    rid, file_id = final_result
    if file_id != FILE_IDS["Schedule"]:
        raise RuntimeError(
            f"IC35 meldete unerwartete File-ID 0x{file_id:02X} für Schedule."
        )
    return rid, file_id


def update_schedule_record(ser, fd: bytes, record_id: int, fields: dict) -> tuple[int, int]:
    """Update an existing Schedule record while preserving its Record-ID."""
    record_id = int(record_id)
    if record_id <= 0 or record_id > 0xFFFFFF:
        raise ValueError(f"Ungültige IC35 Schedule Record-ID für Update: {record_id}")

    data = encode_schedule_record(fields, logger=log)
    total = len(data)
    offset = 0
    chunk_no = 0
    final_result = None
    uid = record_id.to_bytes(3, "little") + bytes([FILE_IDS["Schedule"]])

    while offset < total:
        chunk_no += 1
        chunk = data[offset:offset + 80]
        last = offset + len(chunk) >= total

        if offset == 0:
            l4 = (
                b"\x01\x09" + fd + uid +
                b"\x10\x00\x00" + b"\x00\x00" +
                total.to_bytes(4, "little") + chunk
            )
        else:
            l4 = chunk

        l2_id = 0x82 if last else 0x02
        l3_id = 0x49 if last else 0x48
        request = make_fragment_command(l2_id, l3_id, l4)
        label = f"Update Schedule Block {chunk_no} ({'last' if last else 'more'})"
        log(
            f"{label}: Record-ID={record_id}, Record-Bytes {offset}..{offset+len(chunk)-1} "
            f"von {total}, L2=0x{l2_id:02X}, L3=0x{l3_id:02X}"
        )
        response = l1_transaction(ser, request, label)
        if response is None:
            raise RuntimeError(f"Keine gültige Antwort auf {label}.")
        decoded = _decode_write_response(response, last, label)
        if decoded is None:
            raise RuntimeError(f"IC35 hat {label} nicht bestätigt.")
        if last:
            final_result = decoded
        offset += len(chunk)

    if final_result is None:
        raise RuntimeError("IC35 Schedule-Update endete ohne Record-ID-Antwort.")

    rid, file_id = final_result
    if rid != record_id:
        raise RuntimeError(
            f"IC35 meldete nach Schedule-Update Record-ID {rid}, erwartet war {record_id}."
        )
    if file_id != FILE_IDS["Schedule"]:
        raise RuntimeError(
            f"IC35 meldete unerwartete File-ID 0x{file_id:02X} für Schedule-Update."
        )
    return rid, file_id

def make_fragment_command(l2_id: int, l3_id: int, l4_data: bytes) -> bytes:
    return make_l2(l2_id, make_l3(l3_id, l4_data))


def _decode_write_response(l2: bytes, last: bool, label: str):
    if not l2 or len(l2) < 3:
        log(f"FEHLER {label}: leere/zu kurze Schreibantwort.")
        return None
    l2_id = l2[0]
    l2_len = int.from_bytes(l2[1:3], "little")
    log(f"{label}: Schreibantwort L2=0x{l2_id:02X}, Laenge={l2_len}")
    if l2_len != len(l2):
        log(f"WARNUNG {label}: L2-Laenge {l2_len}, tatsächlich {len(l2)}")
    if not last:
        if l2_id == 0x90 and len(l2) == 3:
            log(f"{label}: IC35 fordert nächsten Schreibblock an (0x90).")
            return (None, None)
        log(f"FEHLER {label}: erwartet 90 03 00, erhalten {hx(l2)}")
        return None
    if l2_id != 0xA0 or len(l2) < 10:
        log(f"FEHLER {label}: ungültige letzte Schreibantwort: {hx(l2)}")
        return None
    l3_id = l2[3]
    l3_len = int.from_bytes(l2[4:6], "little")
    if l3_id != 0x49 or l3_len < 7 or len(l2) < 10:
        log(f"FEHLER {label}: erwartete A0/49-Schreibantwort, erhalten {hx(l2)}")
        return None
    uid = l2[6:10]
    rid = int.from_bytes(uid[:3], "little")
    file_id = uid[3]
    log(f"{label}: IC35 Record-ID={rid} (0x{rid:06X}), File-ID=0x{file_id:02X}")
    return rid, file_id


def write_new_address_record(ser, fd: bytes, fields: dict) -> tuple[int, int]:
    """Write one new Addresses record, byte-for-byte following ic35link 1.18.

    Original ic35link splits *record data* into chunks of at most 80 bytes.
    First chunk carries command 01 08, FD, zero UID, magic 51 00 00, two
    zero bytes and the 32-bit total record length.  Further chunks contain
    only remaining record bytes.
    """
    data = encode_address_record(fields, logger=log)
    total = len(data)
    offset = 0
    chunk_no = 0
    final_result = None
    while offset < total:
        chunk_no += 1
        chunk = data[offset:offset + 80]
        last = offset + len(chunk) >= total
        if offset == 0:
            l4 = (
                b"\x01\x08" + fd + b"\x00\x00\x00\x00" +
                b"\x51\x00\x00" + b"\x00\x00" +
                total.to_bytes(4, "little") + chunk
            )
        else:
            l4 = chunk
        l2_id = 0x82 if last else 0x02
        l3_id = 0x49 if last else 0x48
        request = make_fragment_command(l2_id, l3_id, l4)
        label = f"Write Addresses Block {chunk_no} ({'last' if last else 'more'})"
        log(
            f"{label}: Record-Bytes {offset}..{offset+len(chunk)-1} "
            f"von {total}, L2=0x{l2_id:02X}, L3=0x{l3_id:02X}"
        )
        response = l1_transaction(ser, request, label)
        if response is None:
            raise RuntimeError(f"Keine gültige Antwort auf {label}.")
        decoded = _decode_write_response(response, last, label)
        if decoded is None:
            raise RuntimeError(f"IC35 hat {label} nicht bestätigt.")
        if last:
            final_result = decoded
        offset += len(chunk)
    if final_result is None:
        raise RuntimeError("IC35-Schreibvorgang endete ohne neue Record-ID.")
    rid, file_id = final_result
    if file_id != FILE_IDS["Addresses"]:
        raise RuntimeError(
            f"IC35 meldete unerwartete File-ID 0x{file_id:02X} für Addresses."
        )
    return rid, file_id


def update_address_record(ser, fd: bytes, record_id: int, fields: dict) -> tuple[int, int]:
    """Update an existing Addresses record while keeping its record-ID.

    This follows ic35link 1.18 CMDfupdrec (01 09).  Like write-new, the
    serialized record data is split into chunks of at most 80 bytes.  The
    first block carries FD, the existing 3-byte record-ID + file-ID, the
    historic Addresses magic 51 00 00, two zero bytes and the total record
    length.  Continuation blocks use the normal 0x90 write-more transport.
    """
    record_id = int(record_id)
    if record_id <= 0 or record_id > 0xFFFFFF:
        raise ValueError(f"Ungültige IC35 Record-ID für Update: {record_id}")

    data = encode_address_record(fields, logger=log)
    total = len(data)
    offset = 0
    chunk_no = 0
    final_result = None
    uid = record_id.to_bytes(3, "little") + bytes([FILE_IDS["Addresses"]])

    while offset < total:
        chunk_no += 1
        chunk = data[offset:offset + 80]
        last = offset + len(chunk) >= total
        if offset == 0:
            l4 = (
                b"\x01\x09" + fd + uid +
                b"\x51\x00\x00" + b"\x00\x00" +
                total.to_bytes(4, "little") + chunk
            )
        else:
            l4 = chunk

        l2_id = 0x82 if last else 0x02
        l3_id = 0x49 if last else 0x48
        request = make_fragment_command(l2_id, l3_id, l4)
        label = f"Update Addresses Block {chunk_no} ({'last' if last else 'more'})"
        log(
            f"{label}: Record-ID={record_id}, Record-Bytes {offset}..{offset+len(chunk)-1} "
            f"von {total}, L2=0x{l2_id:02X}, L3=0x{l3_id:02X}"
        )
        response = l1_transaction(ser, request, label)
        if response is None:
            raise RuntimeError(f"Keine gültige Antwort auf {label}.")
        decoded = _decode_write_response(response, last, label)
        if decoded is None:
            raise RuntimeError(f"IC35 hat {label} nicht bestätigt.")
        if last:
            final_result = decoded
        offset += len(chunk)

    if final_result is None:
        raise RuntimeError("IC35-Update endete ohne Record-ID-Antwort.")
    rid, file_id = final_result
    if rid != record_id:
        raise RuntimeError(
            f"IC35 meldete nach Update Record-ID {rid}, erwartet war {record_id}."
        )
    if file_id != FILE_IDS["Addresses"]:
        raise RuntimeError(
            f"IC35 meldete unerwartete File-ID 0x{file_id:02X} für Addresses-Update."
        )
    return rid, file_id


def delete_address_record(ser, fd: bytes, record_id: int) -> bool:
    """Delete exactly one Addresses record using documented CMDfdelrec (01 02).

    Protocol payload: 01 02 + fd(2 LE) + record-id(3 LE) + file-id(05).
    A successful delete returns the short Level-2 response A0 03 00, which
    command_transaction decodes to an empty payload (b"").
    """
    record_id = int(record_id)
    if record_id <= 0 or record_id > 0xFFFFFF:
        raise ValueError(f"Ungültige IC35 Record-ID für Delete: {record_id}")
    l4 = (
        b"\x01\x02" + fd + record_id.to_bytes(3, "little") +
        bytes([FILE_IDS["Addresses"]])
    )
    response = command_transaction(
        ser, 0x83, l4, f"Delete Addresses Record-ID {record_id}"
    )
    if response is None:
        raise RuntimeError(f"IC35 hat Delete für Record-ID {record_id} nicht bestätigt.")
    log(f"Delete Addresses Record-ID {record_id}: IC35 response done (A0).")
    return True


def verify_address_record_absent(ser, fd: bytes, record_id: int, expected_count: int | None = None) -> int:
    """Verify a directly deleted contact no longer appears in the Addresses file.

    We intentionally do not rely on read-by-id error semantics.  Instead the
    database count is read and every remaining record is enumerated by index.
    This also verifies that a direct protocol delete did not merely create a
    ChangeFlag=0x20 tombstone.
    """
    record_id = int(record_id)
    count = read_database_count(ser, "Addresses", fd)
    if count is None:
        raise RuntimeError("Addresses-Anzahl nach Delete konnte nicht gelesen werden.")
    if expected_count is not None and count != int(expected_count):
        raise RuntimeError(
            f"Addresses-Anzahl nach Delete ist {count}, erwartet war {expected_count}."
        )
    remaining = []
    for idx in range(count):
        raw = read_record_raw_by_index(ser, "Addresses", fd, idx)
        if raw is None:
            raise RuntimeError(f"Addresses Record #{idx} nach Delete nicht lesbar.")
        rec = decode_record("Addresses", raw, idx)
        if not rec:
            raise RuntimeError(f"Addresses Record #{idx} nach Delete nicht dekodierbar.")
        rid = int(rec.get("record_id", -1))
        remaining.append(rid)
        if rid == record_id:
            raise RuntimeError(
                f"Delete-Verifikation fehlgeschlagen: Record-ID {record_id} ist noch vorhanden."
            )
    log(
        f"DELETE READ-BACK VERIFIZIERT: Record-ID {record_id} fehlt; "
        f"Addresses enthält jetzt {count} Record(s): {remaining}."
    )
    return count



def delete_schedule_record(ser, fd: bytes, record_id: int) -> bool:
    """Delete exactly one Schedule record using CMDfdelrec (01 02)."""
    record_id = int(record_id)
    if record_id <= 0 or record_id > 0xFFFFFF:
        raise ValueError(f"Ungültige IC35 Schedule Record-ID für Delete: {record_id}")
    l4 = (
        b"\x01\x02" + fd + record_id.to_bytes(3, "little") +
        bytes([FILE_IDS["Schedule"]])
    )
    response = command_transaction(
        ser, 0x83, l4, f"Delete Schedule Record-ID {record_id}"
    )
    if response is None:
        raise RuntimeError(
            f"IC35 hat Schedule-Delete für Record-ID {record_id} nicht bestätigt."
        )
    log(f"Delete Schedule Record-ID {record_id}: IC35 response done (A0).")
    return True


def verify_schedule_record_absent(
    ser, fd: bytes, record_id: int, expected_count: int | None = None
) -> int:
    """Verify a directly deleted Schedule record is completely absent."""
    record_id = int(record_id)
    count = read_database_count(ser, "Schedule", fd)
    if count is None:
        raise RuntimeError("Schedule-Anzahl nach Delete konnte nicht gelesen werden.")
    if expected_count is not None and count != int(expected_count):
        raise RuntimeError(
            f"Schedule-Anzahl nach Delete ist {count}, erwartet war {expected_count}."
        )

    remaining = []
    for idx in range(count):
        raw = read_record_raw_by_index(ser, "Schedule", fd, idx)
        if raw is None:
            raise RuntimeError(f"Schedule Record #{idx} nach Delete nicht lesbar.")
        rec = decode_record("Schedule", raw, idx)
        if not rec:
            raise RuntimeError(f"Schedule Record #{idx} nach Delete nicht dekodierbar.")
        rid = int(rec.get("record_id", -1))
        remaining.append(rid)
        if rid == record_id:
            raise RuntimeError(
                f"Schedule-Delete-Verifikation fehlgeschlagen: "
                f"Record-ID {record_id} ist noch vorhanden."
            )

    log(
        f"SCHEDULE DELETE READ-BACK VERIFIZIERT: Record-ID {record_id} fehlt; "
        f"Schedule enthält jetzt {count} Record(s): {remaining}."
    )
    return count

def read_record_raw_by_id(ser, filename: str, fd: bytes, record_id: int):
    file_id = FILE_IDS[filename]
    l4 = (
        b"\x01\x05" + fd + int(record_id).to_bytes(3, "little") +
        bytes([file_id])
    )
    request = make_command(0x83, l4)
    label = f"Read {filename} Record-ID {record_id}"
    l2 = l1_transaction(ser, request, label)
    if l2 is None:
        return None
    parsed = parse_record_l2_chunk(l2, label)
    if parsed is None:
        return None
    state, payload = parsed
    data = bytearray(payload)
    fragment_no = 1
    while state == "more":
        fragment_no += 1
        if fragment_no > 64:
            raise RuntimeError(f"{label}: zu viele Fragmente.")
        more_label = f"Read more {filename} Record-ID {record_id} #{fragment_no}"
        l2 = l1_transaction(ser, b"\x83", more_label)
        if l2 is None:
            return None
        parsed = parse_record_l2_chunk(l2, more_label)
        if parsed is None:
            return None
        state, payload = parsed
        data.extend(payload)
    return bytes(data)


def disconnect(ser) -> bool:
    """Documented Level-2 disconnect (81 03 00 -> A0 03 00)."""
    response = l1_transaction(ser, b"\x81\x03\x00", "Disconnect")
    if response == b"\xA0\x03\x00":
        log("Disconnect: OK")
        return True
    if response is not None:
        log(f"WARNUNG Disconnect: unerwartete Antwort {hx(response)}")
    return False

# ---------------------------------------------------------------------------
# Connection / session
# ---------------------------------------------------------------------------

def do_welcome_and_reopen(ser) -> bool:
    log()
    log("=== 1. Welcome-Phase ===")
    log("Bitte jetzt den Sync-Knopf an der SyncStation druecken.")

    deadline = time.monotonic() + 45.0
    rxbuf = bytearray()
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        tx(ser, WELCOME_CHAR, f"Welcome Versuch {attempt:02d}")

        incoming = read_some(ser, 1.15)
        if incoming:
            log(f"RX Welcome: {hx(incoming)}  {incoming!r}")
            rxbuf.extend(incoming)

            if len(rxbuf) > 256:
                del rxbuf[:-256]

            if WELCOME_TEXT in rxbuf:
                log("WELCOME erkannt.")
                break
    else:
        log("FEHLER: Kein WELCOME innerhalb von 45 Sekunden.")
        return False

    ser.reset_input_buffer()

    tx(ser, WELCOME_CHAR, "Welcome Abschluss")
    ok, _ = wait_for_byte(ser, 0x80, 2.0, "Welcome Ready")

    if not ok:
        log("FEHLER: Nach WELCOME kam kein 0x80.")
        return False

    log("Welcome-Handshake komplett: OK")

    log()
    log("=== 1b. Historischer Windows-Portwechsel ===")

    try:
        ser.dtr = False
        log("DTR -> AUS")
    except Exception as exc:
        log(f"WARNUNG DTR AUS: {exc}")

    ser.close()
    log(f"{PORT} geschlossen.")

    time.sleep(0.020)

    try:
        ser.rts = False
        ser.dtr = True
    except Exception:
        pass

    ser.open()
    log(f"{PORT} wieder geoeffnet.")

    try:
        ser.rts = False
        log("RTS -> AUS")
    except Exception as exc:
        log(f"WARNUNG RTS AUS: {exc}")

    try:
        ser.dtr = True
        log("DTR -> EIN")
    except Exception as exc:
        log(f"WARNUNG DTR EIN: {exc}")

    ser.reset_input_buffer()
    ser.reset_output_buffer()
    time.sleep(0.050)

    log("Windows-Portwechsel abgeschlossen.")
    return True


def init_level1(ser) -> bool:
    log()
    log("=== 2. Level-1 initialisieren ===")

    tx(ser, L1_INIT, "L1 init")
    ok, _ = wait_for_byte(ser, 0xF0, 2.0, "L1 ack0")

    if not ok:
        log("FEHLER: Kein F0 auf L1 init.")
        return False

    log("Level-1 init: OK")
    return True


def identify(ser):
    log()
    log("=== 2. IC35 identifizieren ===")

    response = l1_transaction(ser, IDENTIFY_L2, "Identify")
    if response is None:
        return None

    marker = b"INVENTEC"
    pos = response.find(marker)

    if pos < 0:
        log("FEHLER: INVENTEC-Kennung nicht gefunden.")
        log(f"Identify L2: {hx(response)}")
        return None

    raw = response[pos:]
    raw = bytes(b for b in raw if 0x20 <= b <= 0x7E)
    identity = raw.decode("ascii", errors="replace")

    log(f"IC35: {identity}")
    return identity


# ---------------------------------------------------------------------------
# Read-only organizer probe
# ---------------------------------------------------------------------------

def power_request(ser) -> bool:
    log()
    log("=== 3. Power-Request ===")

    l4 = b"\x03\x01" + b"Power" + b"\x00\x00\x00"
    response = command_transaction(ser, 0x82, l4, "Power")

    if response is None:
        return False

    log(f"Power Antwortdaten: {hx(response)}")

    if response[:2] == b"\x03\x01":
        log("Power-Request: OK")
        return True

    log("WARNUNG: Power-Antwort weicht vom dokumentierten Muster ab.")
    return True


def authenticate_once(ser, password: str):
    # Passwort niemals in das Log schreiben.
    password_bytes = password.encode("latin-1", errors="replace")
    l4 = b"\x03\x00" + password_bytes

    return command_transaction(
        ser,
        0x82,
        l4,
        "Authentication",
        sensitive=True,
    )


def authenticate(ser) -> bool:
    log()
    log("=== 4. Authentication ===")
    log("Versuche zuerst ohne Kennwort.")

    response = authenticate_once(ser, "")

    if response is None:
        return False

    log(f"Authentication Antwort: {hx(response)}")

    if response[:2] == b"\x01\x01":
        log("Authentication: OK (kein Kennwort erforderlich).")
        return True

    if response[:2] != b"\x00\x01":
        log("WARNUNG: Unbekannte Authentication-Antwort.")
        return True

    log()
    log("Der IC35 verlangt offenbar ein Kennwort.")
    log("Das Kennwort wird NICHT angezeigt und NICHT ins Log geschrieben.")

    for attempt in range(1, 4):
        password = getpass.getpass(
            f"IC35-Kennwort eingeben (Versuch {attempt}/3): "
        )

        if not password:
            log("Keine Eingabe. Authentication abgebrochen.")
            return False

        response = authenticate_once(ser, password)

        # Passwortvariable moeglichst frueh verwerfen.
        password = None

        if response is None:
            return False

        log(f"Authentication Antwort: {hx(response)}")

        if response[:2] == b"\x01\x01":
            log("Authentication: OK.")
            return True

        if response[:2] == b"\x00\x01":
            log("Kennwort wurde vom IC35 abgelehnt.")
            continue

        log("WARNUNG: Unbekannte Authentication-Antwort.")
        return False

    log("Authentication nach 3 Versuchen nicht erfolgreich.")
    return False


def read_sync_info(ser):
    log()
    log("=== 5. Sync-Systeminfo lesen ===")

    # Dokumentiert als "get date+time":
    # 83,49 02 00 00
    response = command_transaction(
        ser,
        0x83,
        b"\x02\x00\x00",
        "Get date+time",
    )

    if response is None:
        return

    log(f"Systeminfo roh: {hx(response)}")

    # Dokumentiert: mmddyyyyhhmmss + 00 00.
    ascii_part = bytes(b for b in response if 0x30 <= b <= 0x39)

    if len(ascii_part) >= 14:
        s = ascii_part[:14].decode("ascii")
        mm, dd, yyyy = s[0:2], s[2:4], s[4:8]
        hh, mi, ss = s[8:10], s[10:12], s[12:14]
        log(
            f"Gespeicherter Sync-Zeitstempel: "
            f"{dd}.{mm}.{yyyy} {hh}:{mi}:{ss}"
        )
    elif all(b == 0 for b in response):
        log("Sync-Systeminfo ist leer/noch nicht gesetzt.")
    else:
        log("Sync-Systeminfo konnte nicht als Datum interpretiert werden.")


def make_open_file_l4(filename: str) -> bytes:
    name = filename.encode("ascii")
    lf = len(name)

    # Dokumentiertes Format:
    # 00 02
    # 00 00 00 00 00 00 00
    # (lf+2)
    # 00 00 00
    # lf
    # filename
    # 02
    return (
        b"\x00\x02"
        + b"\x00" * 7
        + bytes([lf + 2])
        + b"\x00" * 3
        + bytes([lf])
        + name
        + b"\x02"
    )


def open_database(ser, filename: str):
    response = command_transaction(
        ser,
        0x82,
        make_open_file_l4(filename),
        f"Open {filename}",
    )

    if response is None or len(response) < 2:
        log(f"FEHLER: {filename} konnte nicht geoeffnet werden.")
        return None

    fd = response[:2]
    log(
        f"{filename}: Filedescriptor "
        f"0x{int.from_bytes(fd, 'little'):04X}"
    )
    return fd


def read_database_count(ser, filename: str, fd: bytes):
    # 01 03 = total number of records
    l4 = b"\x01\x03" + fd + b"\x00\x00\x00\x00"

    response = command_transaction(
        ser,
        0x83,
        l4,
        f"Count {filename}",
    )

    if response is None or len(response) < 2:
        log(f"FEHLER: Datensatzanzahl fuer {filename} nicht erhalten.")
        return None

    count = int.from_bytes(response[:2], "little")
    log(f"{filename}: {count} Datensaetze")
    return count


def close_database(ser, filename: str, fd: bytes):
    l4 = b"\x00\x03" + fd + b"\x00\x00\x00\x00"

    response = command_transaction(
        ser,
        0x82,
        l4,
        f"Close {filename}",
    )

    if response is None:
        log(f"WARNUNG: Keine Close-Antwort fuer {filename}.")
        return False

    log(f"{filename}: geschlossen.")
    return True


def parse_record_l2_chunk(l2: bytes, label: str):
    """
    Dekodiert einen L2/L3-Block einer Record-Antwort.

    Dokumentierte Antwortformen:
      20 ll ll 48 ll ll <record/fdata>   = more
      A0 ll ll 49 ll ll <record/fdata>   = last
    """
    if len(l2) < 6:
        log(f"FEHLER {label}: Record-L2-Antwort zu kurz: {hx(l2)}")
        return None

    l2_id = l2[0]
    l2_len = int.from_bytes(l2[1:3], "little")
    l3_id = l2[3]
    l3_len = int.from_bytes(l2[4:6], "little")

    if l2_len != len(l2):
        log(
            f"WARNUNG {label}: L2-Laenge={l2_len}, "
            f"tatsaechlich={len(l2)}"
        )

    if l3_len < 3 or 3 + l3_len > len(l2):
        log(
            f"FEHLER {label}: unplausible L3-Laenge "
            f"{l3_len} bei {len(l2)} L2-Bytes."
        )
        return None

    payload = l2[6:3 + l3_len]

    if l2_id == 0x20 and l3_id == 0x48:
        state = "more"
    elif l2_id == 0xA0 and l3_id == 0x49:
        state = "last"
    else:
        log(
            f"WARNUNG {label}: unerwartete Fragment-IDs "
            f"L2=0x{l2_id:02X}, L3=0x{l3_id:02X}"
        )
        state = "unknown"

    log(
        f"{label}: Fragment={state}, L2=0x{l2_id:02X}, "
        f"L3=0x{l3_id:02X}, Nutzdaten={len(payload)} Bytes"
    )

    return state, payload


def read_record_raw_by_index(ser, filename: str, fd: bytes, index: int):
    # 01 06 fd idx 00 00
    l4 = (
        b"\x01\x06"
        + fd
        + index.to_bytes(2, "little")
        + b"\x00\x00"
    )

    request = make_command(0x83, l4)
    label = f"Read {filename} index {index}"

    l2 = l1_transaction(ser, request, label)
    if l2 is None:
        return None

    parsed = parse_record_l2_chunk(l2, label)
    if parsed is None:
        return None

    state, payload = parsed
    data = bytearray(payload)

    # Falls ein Record groesser als ein Antwortblock ist, fordert die
    # Protokolldoku weitere Fragmente mit der kurzen L2-PDU 0x83 an.
    fragment_no = 1
    while state == "more":
        fragment_no += 1
        if fragment_no > 64:
            log(f"FEHLER {label}: zu viele Record-Fragmente.")
            return None

        more_label = f"Read more {filename} index {index} #{fragment_no}"
        l2 = l1_transaction(ser, b"\x83", more_label)

        if l2 is None:
            return None

        parsed = parse_record_l2_chunk(l2, more_label)
        if parsed is None:
            return None

        state, payload = parsed
        data.extend(payload)

    if state not in ("last", "unknown"):
        log(f"FEHLER {label}: Record wurde nicht sauber abgeschlossen.")
        return None

    log(f"{label}: kompletter Record-Rohblock = {len(data)} Bytes")
    return bytes(data)


def decode_text_field(data: bytes) -> str:
    # Der IC35 stammt aus der westeuropaeischen Windows-Aera. cp1252
    # ist fuer die Anzeige deutscher Umlaute robuster als ASCII.
    # Rohbytes bleiben zusaetzlich im JSON erhalten.
    return data.decode("cp1252", errors="replace").rstrip("\x00")


def decode_record(filename: str, raw: bytes, index: int):
    specs = FIELD_SPECS[filename]

    if len(raw) < 5:
        log(f"FEHLER {filename}[{index}]: Recordheader zu kurz.")
        return None

    record_id_bytes = raw[0:3]
    record_id = int.from_bytes(record_id_bytes, "little")
    file_id = raw[3]
    change_flag = raw[4]

    result = {
        "index": index,
        "record_id": record_id,
        "record_id_hex": hx(record_id_bytes),
        "file_id": file_id,
        "file_id_hex": f"{file_id:02X}",
        "change_flag": change_flag,
        "change_flag_hex": f"{change_flag:02X}",
        "raw_hex": hx(raw),
        "fields": {},
    }

    expected_file_id = FILE_IDS.get(filename)
    if expected_file_id is not None and file_id != expected_file_id:
        log(
            f"WARNUNG {filename}[{index}]: File-ID 0x{file_id:02X}, "
            f"erwartet 0x{expected_file_id:02X}."
        )

    # Geloeschte Records haben laut Doku keine Feldlaengen/Felddaten.
    if change_flag == 0x20:
        result["deleted"] = True
        log(f"{filename}[{index}]: geloeschter Record (ChangeFlag 20).")
        return result

    nfields = len(specs)
    if len(raw) < 5 + nfields:
        log(
            f"FEHLER {filename}[{index}]: "
            f"Feldlaengentabelle unvollstaendig."
        )
        return result

    lengths = list(raw[5:5 + nfields])
    fdata = raw[5 + nfields:]
    needed = sum(lengths)

    result["field_lengths"] = lengths
    result["field_data_hex"] = hx(fdata)

    if needed > len(fdata):
        log(
            f"FEHLER {filename}[{index}]: Feldlaengen verlangen {needed} "
            f"Bytes, vorhanden sind nur {len(fdata)}."
        )
        return result

    if needed < len(fdata):
        result["trailing_hex"] = hx(fdata[needed:])
        log(
            f"HINWEIS {filename}[{index}]: "
            f"{len(fdata)-needed} zusaetzliche Byte(s) nach Felddaten."
        )

    pos = 0
    for (field_name, field_type), field_len in zip(specs, lengths):
        value_raw = fdata[pos:pos + field_len]
        pos += field_len

        if field_type == "bin":
            if len(value_raw) == 0:
                value = None
            elif len(value_raw) == 1:
                value = value_raw[0]
            else:
                value = int.from_bytes(value_raw, "little")

            field_entry = {
                "type": "binary",
                "length": field_len,
                "value": value,
                "hex": hx(value_raw),
            }
        else:
            value = decode_text_field(value_raw)
            field_entry = {
                "type": "text",
                "length": field_len,
                "value": value,
                "hex": hx(value_raw),
            }

        result["fields"][field_name] = field_entry

    return result


def log_decoded_record(filename: str, record: dict):
    log()
    log(
        f"{filename} Record #{record['index']}  "
        f"Record-ID={record['record_id']} "
        f"(0x{record['record_id']:06X}), "
        f"File-ID=0x{record['file_id']:02X}, "
        f"Change=0x{record['change_flag']:02X}"
    )

    if record.get("deleted"):
        log("  <geloescht>")
        return

    fields = record.get("fields", {})
    for field_name, entry in fields.items():
        if entry["type"] == "binary":
            if entry["length"] == 0:
                display = "<leer>"
            else:
                display = f"{entry['value']} (hex {entry['hex']})"
        else:
            display = entry["value"]
            if display == "":
                display = "<leer>"

        log(f"  {field_name:14s}: {display}")


def read_and_export_databases(ser, identity: str):
    log()
    log("=== 6. Organizer-Records vollstaendig lesen ===")

    export = {
        "format": "IC35 Organizer read-only dump",
        "reader_version": "0.7",
        "device": identity,
        "port": PORT,
        "baudrate": BAUDRATE,
        "databases": {},
    }

    counts = {}

    for filename in DATABASES:
        log()
        log("=" * 62)
        log(f" DATENBANK: {filename}")
        log("=" * 62)

        fd = open_database(ser, filename)
        if fd is None:
            export["databases"][filename] = {
                "error": "open failed",
                "records": [],
            }
            counts[filename] = None
            continue

        db_records = []

        try:
            count = read_database_count(ser, filename, fd)
            counts[filename] = count

            if count is None:
                export["databases"][filename] = {
                    "error": "count failed",
                    "records": [],
                }
                continue

            log(f"{filename}: lese jetzt {count} Record(s) per Index.")

            for index in range(count):
                raw = read_record_raw_by_index(ser, filename, fd, index)

                if raw is None:
                    log(f"FEHLER: {filename} Record index {index} nicht gelesen.")
                    db_records.append({
                        "index": index,
                        "error": "read failed",
                    })
                    continue

                record = decode_record(filename, raw, index)

                if record is None:
                    db_records.append({
                        "index": index,
                        "error": "decode failed",
                        "raw_hex": hx(raw),
                    })
                    continue

                db_records.append(record)
                log_decoded_record(filename, record)

            export["databases"][filename] = {
                "count": count,
                "records": db_records,
            }

        finally:
            close_database(ser, filename, fd)

    EXPORTFILE.write_text(
        json.dumps(export, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log()
    log("=" * 62)
    log(" READ-ONLY EXPORT ABGESCHLOSSEN")
    log("=" * 62)

    for filename in DATABASES:
        value = counts.get(filename)
        if value is None:
            log(f" {filename:12s}: Fehler")
        else:
            log(f" {filename:12s}: {value}")

    log("=" * 62)
    log(f"JSON-Export: {EXPORTFILE}")
    log("Es wurden KEINE Records geschrieben/geaendert/geloescht.")

    return export

def best_effort_exit(ser):
    """
    Erfolgreiche L2-Operationen beenden ihren eigenen Level-1-Zyklus bereits.
    Falls ein Fehler mitten in einer Transaktion passiert ist, senden wir
    vorsichtig noch ein einzelnes L1-exit als Aufraeumversuch.
    """
    if not ser.is_open:
        return

    try:
        log()
        log("=== Abschluss / Sicherheits-Exit ===")
        ser.reset_input_buffer()
        tx(ser, L1_EXIT, "L1 safety exit")
        ok, seen = wait_for_byte(ser, 0xF1, 1.0, "L1 safety exit ack1")

        if ok:
            log("Safety-Exit mit F1 bestaetigt.")
        else:
            log("Kein F1 auf Safety-Exit; Port wird normal geschlossen.")
    except Exception as exc:
        log(f"Exit-Hinweis: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    LOGFILE.write_text("", encoding="utf-8")

    log("=" * 62)
    log(" Siemens IC35 Sync - Organizer Reader v0.7")
    log("=" * 62)
    log(f" Port:       {PORT}")
    log(f" Baudrate:   {BAUDRATE}")
    log(" Format:     8 Datenbits, keine Paritaet, 2 Stopbits")
    log(" Modus:      READ-ONLY")
    log(" Liest Records, schreibt aber KEINE Organizer-Daten/Change-Flags.")
    log(f" Logdatei:   {LOGFILE.name}")
    log("=" * 62)

    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0.10,
            write_timeout=0.75,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
    except serial.SerialException as exc:
        log(f"FEHLER: {PORT} konnte nicht geoeffnet werden.")
        log(str(exc))
        input("\nEnter zum Beenden ...")
        return 1

    try:
        try:
            ser.rts = False
            ser.dtr = True
        except Exception:
            pass

        ser.reset_input_buffer()
        ser.reset_output_buffer()

        log(f"{PORT} erfolgreich geoeffnet.")

        if not do_welcome_and_reopen(ser):
            return 2

        log()
        log("Ab jetzt bekommt JEDER L2-Befehl einen eigenen L1-Zyklus.")

        identity = identify(ser)
        if identity is None:
            return 4

        if not power_request(ser):
            return 5

        if not authenticate(ser):
            log("Ohne erfolgreiche Authentication werden keine Datenbanken gelesen.")
            return 6

        read_sync_info(ser)

        export = read_and_export_databases(ser, identity)

        log()
        log("ERFOLG: Organizer-Records wurden read-only ausgelesen.")
        log("Naechster Schritt: Mapping nach vCard/iCalendar fuer Thunderbird.")

        return 0

    except KeyboardInterrupt:
        log("\nAbgebrochen.")
        return 130

    except serial.SerialException as exc:
        log(f"\nSerieller Fehler: {exc}")
        return 7

    except Exception as exc:
        log(f"\nUnerwarteter Fehler: {type(exc).__name__}: {exc}")
        return 8

    finally:
        # Jede regulaere L2-Transaktion wurde bereits mit L1-exit beendet.
        # Kein zusaetzlicher Safety-Exit: v0.6 zeigte, dass dieser im
        # Leerlauf erwartungsgemaess mit F3 abgelehnt wird.
        if ser.is_open:
            ser.close()

        log(f"\n{PORT} geschlossen.")
        log(f"Log gespeichert als: {LOGFILE}")
        input("Enter zum Beenden ...")


if __name__ == "__main__":
    sys.exit(main())
