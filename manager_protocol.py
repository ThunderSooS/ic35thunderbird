from __future__ import annotations

import time
from pathlib import Path

BAUDRATE = 115200
WELCOME_CHAR = b"@"  # 0x40, Manager protocol
WELCOME_TEXT = b"WELCOME"
BACKUP_HEAD_SIZE = 136
BACKUP_BLOCK_SIZE = 16384
BACKUP_BLOCK_COUNT = 26
BACKUP_TOTAL_SIZE = BACKUP_HEAD_SIZE + BACKUP_BLOCK_COUNT * BACKUP_BLOCK_SIZE + 8


def _hx(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _read_exact(ser, count: int, timeout: float) -> bytes:
    end = time.monotonic() + timeout
    out = bytearray()
    while len(out) < count and time.monotonic() < end:
        chunk = ser.read(count - len(out))
        if chunk:
            out.extend(chunk)
        else:
            time.sleep(0.002)
    return bytes(out)


def _read_some(ser, duration: float) -> bytes:
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


def _cmd_rsp(ser, command: int, expected: int, label: str, logger=print,
             timeout: float = 0.55, retries: int = 3) -> bool:
    """Manager one-byte command/response with documented retry behavior."""
    for attempt in range(1, retries + 1):
        logger(f"MGR TX {label}: {command:02X}" + (f" (Versuch {attempt})" if attempt > 1 else ""))
        ser.write(bytes([command]))
        ser.flush()
        rsp = _read_exact(ser, 1, timeout)
        if rsp == bytes([expected]):
            logger(f"MGR RX {label}: {expected:02X}")
            return True
        if rsp:
            logger(f"MGR RX {label}: {_hx(rsp)} (erwartet {expected:02X})")
        else:
            logger(f"MGR RX {label}: <Timeout>")
    return False


def manager_welcome(ser, logger=print, wait_seconds: float = 45.0) -> bool:
    logger("=== Manager: Welcome-Phase ===")
    logger("Bitte den Sync-Knopf an der IC35-SyncStation drücken.")
    deadline = time.monotonic() + wait_seconds
    buf = bytearray()
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        ser.write(WELCOME_CHAR)
        ser.flush()
        logger(f"MGR TX Welcome {attempt:02d}: 40 ('@')")
        incoming = _read_some(ser, 1.15)
        if incoming:
            logger(f"MGR RX Welcome: {_hx(incoming)}  {incoming!r}")
            buf.extend(incoming)
            if len(buf) > 256:
                del buf[:-256]
            if WELCOME_TEXT in buf:
                break
    else:
        logger("FEHLER: Manager-WELCOME nicht empfangen.")
        return False

    ser.reset_input_buffer()
    ser.write(WELCOME_CHAR)
    ser.flush()
    logger("MGR TX Verbindungsstart: 40")
    ready = _read_exact(ser, 1, 2.0)
    if ready != b"\x80":
        logger(f"FEHLER: Manager-Verbindungsstart: {_hx(ready) if ready else '<Timeout>'}, erwartet 80")
        return False
    logger("MGR RX Verbindungsstart: 80")
    return True


def manager_connect(ser, logger=print) -> str:
    if not manager_welcome(ser, logger):
        raise RuntimeError("Manager-WELCOME-Handshake fehlgeschlagen.")

    if not _cmd_rsp(ser, 0x09, 0x90, "Reset", logger, timeout=0.6, retries=3):
        raise RuntimeError("Manager-Reset fehlgeschlagen.")

    if not _cmd_rsp(ser, 0x10, 0x90, "Identifikation", logger, timeout=0.6, retries=3):
        raise RuntimeError("Manager-Identifikationskommando fehlgeschlagen.")

    identity = _read_exact(ser, 8, 2.0)
    logger(f"MGR RX Gerätekennung: {_hx(identity)}  {identity!r}")
    if identity != b"DCS_SDK\x00":
        raise RuntimeError(f"Unerwartete Manager-Gerätekennung: {identity!r}")

    # Dokumentierte Initialisierung. Eine 0x90-Antwort kann kommen, muss aber nicht.
    ser.write(b"\x50")
    ser.flush()
    logger("MGR TX Initialisierung: 50")
    init_rsp = _read_some(ser, 0.12)
    if init_rsp:
        logger(f"MGR RX Initialisierung (wird verworfen): {_hx(init_rsp)}")
    else:
        logger("MGR RX Initialisierung: <keine Antwort, laut Protokoll zulässig>")
    time.sleep(0.02)
    return "DCS_SDK"


def _ack_block(ser, good: bool, logger=print) -> bool:
    command = 0x60 if good else 0x62
    label = "Block ACK+" if good else "Block ACK-"
    return _cmd_rsp(ser, command, 0xA0, label, logger, timeout=1.0, retries=3)


def _receive_fixed_block(ser, size: int, label: str, logger=print,
                         retries: int = 3) -> bytes:
    for attempt in range(1, retries + 1):
        logger(f"{label}: empfange {size} Byte" + (f" (Wiederholung {attempt})" if attempt > 1 else ""))
        # 16 KiB need roughly 1.4 s at 115200/8N2; allow ample margin for USB adapters.
        data_timeout = 4.0 if size <= 136 else 8.0
        data = _read_exact(ser, size, data_timeout)
        checksum_raw = _read_exact(ser, 2, 2.0)

        if len(data) != size or len(checksum_raw) != 2:
            logger(f"{label}: unvollständig ({len(data)}/{size} Byte, Checksum {len(checksum_raw)}/2)")
            if not _ack_block(ser, False, logger):
                raise RuntimeError(f"{label}: negative Quittung wurde nicht bestätigt.")
            continue

        received = int.from_bytes(checksum_raw, "little")
        calculated = sum(data) & 0xFFFF
        if received != calculated:
            logger(f"{label}: Prüfsumme FALSCH empfangen=0x{received:04X}, berechnet=0x{calculated:04X}")
            if not _ack_block(ser, False, logger):
                raise RuntimeError(f"{label}: negative Quittung wurde nicht bestätigt.")
            continue

        logger(f"{label}: Prüfsumme OK (0x{received:04X})")
        if not _ack_block(ser, True, logger):
            raise RuntimeError(f"{label}: positive Quittung wurde nicht bestätigt.")
        return data

    raise RuntimeError(f"{label}: nach {retries} Versuchen kein gültiger Block.")


def _read_backup_info(ser, label: str, logger=print, timeout: float = 1.0) -> bytes:
    if not _cmd_rsp(ser, 0x18, 0x90, label, logger, timeout=timeout, retries=3):
        raise RuntimeError(f"{label}: Kommando nicht bestätigt.")
    info = _read_exact(ser, 4, 2.0)
    if len(info) != 4:
        raise RuntimeError(f"{label}: nur {len(info)}/4 Infobytes empfangen.")
    logger(f"MGR RX {label} Daten: {_hx(info)}  {info!r}")
    return info


def backup_database(ser, destination: Path, logger=print, progress=None) -> dict:
    """Create the documented database.org-compatible full IC35 backup.

    This is read-only with respect to the IC35 database. PC->IC35 traffic is
    limited to Manager commands and checksum acknowledgements.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    logger("=== Manager: IC35-Komplettbackup ===")
    if not _cmd_rsp(ser, 0x13, 0x90, "Datensicherung", logger, timeout=0.7, retries=3):
        raise RuntimeError("IC35 hat das Backup-Kommando 0x13 nicht bestätigt.")

    chunks = []
    head = _receive_fixed_block(ser, BACKUP_HEAD_SIZE, "Kopfblock", logger)
    chunks.append(head)
    if progress:
        progress(1, BACKUP_BLOCK_COUNT + 1)

    for index in range(BACKUP_BLOCK_COUNT):
        block = _receive_fixed_block(
            ser, BACKUP_BLOCK_SIZE,
            f"Datenblock {index + 1:02d}/{BACKUP_BLOCK_COUNT:02d}", logger,
        )
        chunks.append(block)
        if progress:
            progress(index + 2, BACKUP_BLOCK_COUNT + 1)

    info1 = _read_backup_info(ser, "Backup-Info 1", logger, timeout=1.0)
    info2 = _read_backup_info(ser, "Backup-Info 2", logger, timeout=0.6)
    chunks.extend((info1, info2))

    payload = b"".join(chunks)
    if len(payload) != BACKUP_TOTAL_SIZE:
        raise RuntimeError(
            f"Backup hat falsche Gesamtlänge {len(payload)} statt {BACKUP_TOTAL_SIZE} Byte."
        )

    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(destination)
    logger(f"Backup gespeichert: {destination}")
    logger(f"Backup-Größe: {len(payload)} Byte (erwartet {BACKUP_TOTAL_SIZE})")
    return {
        "path": str(destination),
        "size": len(payload),
        "info1_hex": _hx(info1),
        "info2_hex": _hx(info2),
        "info1_ascii": info1.decode("ascii", errors="replace"),
        "info2_ascii": info2.decode("ascii", errors="replace"),
    }


def manager_disconnect(ser, logger=print) -> None:
    logger("=== Manager: Verbindung beenden ===")
    try:
        _cmd_rsp(ser, 0x09, 0x90, "Reset vor Disconnect", logger, timeout=1.0, retries=3)
    except Exception as exc:
        logger(f"WARNUNG beim Manager-Reset: {exc}")
    try:
        _cmd_rsp(ser, 0x01, 0x90, "Disconnect", logger, timeout=1.0, retries=3)
    except Exception as exc:
        logger(f"WARNUNG beim Manager-Disconnect: {exc}")
