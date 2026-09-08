from __future__ import annotations

import json
import hashlib
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
DEFAULT_CALENDAR_NAME = ""
LOCAL_TZ = ZoneInfo("Europe/Berlin")


def install_credentials(source: Path, target: Path) -> None:
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def get_credentials(credentials_file: Path, token_file: Path, interactive: bool = True):
    """Google OAuth credentials with explicit scope migration.

    OAuth user credentials cannot be expanded by merely attaching a larger
    Python scopes list to an old refresh token. v1.7.4 therefore uses a small
    local scope-version marker. Every older token is re-authorized once.
    """
    scope_marker = token_file.with_name("google_oauth_scope_version.txt")
    required_scope_version = "2"
    creds = None

    # v1.7.0-v1.7.3 may contain a token which was originally granted only
    # calendar.events. v1.7.3's previous has_scopes test was unreliable because
    # from_authorized_user_file(..., SCOPES) overwrote the in-memory scope list.
    # Force one clean re-consent when migrating to scope version 2.
    marker_ok = (
        scope_marker.exists()
        and scope_marker.read_text(encoding="utf-8", errors="ignore").strip()
        == required_scope_version
    )

    if token_file.exists() and not marker_ok:
        try:
            token_file.unlink()
        except FileNotFoundError:
            pass

    if token_file.exists():
        # IMPORTANT: Load the scopes exactly as stored in the token file.
        # Do not pass SCOPES here.
        creds = Credentials.from_authorized_user_file(str(token_file))
        if not creds.has_scopes(SCOPES):
            creds = None
            try:
                token_file.unlink()
            except FileNotFoundError:
                pass
            try:
                scope_marker.unlink()
            except FileNotFoundError:
                pass

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not interactive:
            return None
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"Google OAuth-Datei fehlt: {credentials_file}. "
                "Bitte zuerst eine Desktop-OAuth-Datei (credentials.json) auswählen."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file), SCOPES
        )
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent",
            access_type="offline",
            include_granted_scopes="true",
        )

        # The credential object must at least advertise the scopes requested by
        # this authorization flow. The actual API call is still the final test.
        if not creds.has_scopes(SCOPES):
            raise RuntimeError(
                "Google hat nicht alle benötigten Kalenderrechte erteilt. "
                "Bitte in Google Auth Platform > Datenzugriff die Scopes "
                "'calendar.events' und 'calendar.calendarlist.readonly' hinzufügen "
                "und die Verbindung erneut herstellen."
            )

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        scope_marker.write_text(required_scope_version, encoding="utf-8")

    return creds

def service(credentials_file: Path, token_file: Path, interactive: bool = True):
    creds = get_credentials(credentials_file, token_file, interactive=interactive)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def resolve_calendar_id(svc, summary: str = DEFAULT_CALENDAR_NAME) -> tuple[str, str]:
    token = None
    matches = []
    while True:
        try:
            resp = svc.calendarList().list(maxResults=250, pageToken=token).execute()
        except Exception as exc:
            msg = str(exc)
            if "insufficient" in msg.lower() and ("scope" in msg.lower() or "permission" in msg.lower()):
                raise RuntimeError(
                    "Google verweigert den Zugriff auf die Kalenderliste. "
                    "Öffne Google Cloud Console > Google Auth Platform > Datenzugriff, "
                    "füge die Scopes "
                    "'https://www.googleapis.com/auth/calendar.events' und "
                    "'https://www.googleapis.com/auth/calendar.calendarlist.readonly' hinzu, "
                    "speichere und klicke danach erneut auf 'Google Kalender verbinden'."
                ) from exc
            raise
        for cal in resp.get("items", []):
            if str(cal.get("summary", "")).casefold() == summary.casefold():
                matches.append(cal)
        token = resp.get("nextPageToken")
        if not token:
            break
    if len(matches) != 1:
        raise RuntimeError(
            f"Google-Kalender {summary!r} muss genau einmal vorhanden sein; gefunden: {len(matches)}."
        )
    return matches[0]["id"], matches[0].get("summary", summary)


def connect_calendar_state(state_file: Path, calendar_id: str, calendar_name: str) -> tuple[dict, bool]:
    """Connect without destroying existing Google-event <-> IC35 bindings."""
    old_state = load_state(state_file) if state_file.exists() else {}
    same_calendar = (
        old_state.get("calendar_id") == calendar_id
        and old_state.get("calendar_name", "").casefold() == calendar_name.casefold()
        and old_state.get("baseline_utc")
    )
    if same_calendar:
        state = dict(old_state)
        state["calendar_id"] = calendar_id
        state["calendar_name"] = calendar_name
        state.setdefault("bindings", {})
        save_state(state_file, state)
        return state, False

    state = {
        "calendar_id": calendar_id,
        "calendar_name": calendar_name,
        "baseline_utc": datetime.now(timezone.utc).isoformat(),
        "bindings": {},
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state, True


def save_baseline(state_file: Path, calendar_id: str, calendar_name: str) -> dict:
    state, _created = connect_calendar_state(state_file, calendar_id, calendar_name)
    return state


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def save_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def list_new_events_since_baseline(svc, state: dict) -> list[dict]:
    calendar_id = state.get("calendar_id")
    baseline = state.get("baseline_utc")
    if not calendar_id or not baseline:
        raise RuntimeError("Google-Kalender-Baseline fehlt. Bitte zuerst 'Google Kalender verbinden' ausführen.")
    items = []
    token = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            updatedMin=baseline,
            showDeleted=True,
            singleEvents=False,
            maxResults=2500,
            pageToken=token,
        ).execute()
        items.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    baseline_dt = datetime.fromisoformat(baseline.replace("Z", "+00:00"))
    new_items = []
    bound = set(state.get("bindings", {}).keys())
    for ev in items:
        if ev.get("status") == "cancelled" or ev.get("id") in bound:
            continue
        created = ev.get("created")
        if not created:
            continue
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_dt > baseline_dt:
            new_items.append(ev)
    return new_items


def _google_dt_to_local_compact(part: dict, label: str) -> str:
    if "date" in part:
        raise ValueError(f"{label}: Ganztägige Termine werden im ersten Google-Kalender-Test noch nicht unterstützt.")
    raw = part.get("dateTime")
    if not raw:
        raise ValueError(f"{label}: Start-/Endzeit fehlt.")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%Y%m%dT%H%M%S")


def event_to_ic35_fields(event: dict) -> dict:
    # A recurrence MASTER cannot be represented by the IC35 directly. In the
    # final client we query Google with singleEvents=True, so recurring series
    # arrive as concrete instances (recurringEventId + real start/end). Those
    # instances are safe to mirror as individual one-off IC35 appointments.
    if event.get("recurrence") and not event.get("recurringEventId"):
        raise ValueError(
            "Google-Wiederholungs-Master kann nicht direkt auf den IC35 geschrieben werden."
        )

    # Google reminders are intentionally preserved only on Google. The IC35
    # appointment itself is mirrored without an alarm until an exact alarm
    # mapping has been hardware-tested.
    summary = str(event.get("summary") or "").strip()
    if not summary:
        raise ValueError("Der IC35 benötigt einen Betreff für den Termin.")
    start = _google_dt_to_local_compact(event.get("start") or {}, "START")
    end = _google_dt_to_local_compact(event.get("end") or {}, "ENDE")
    if end < start:
        raise ValueError("Terminende liegt vor Terminbeginn.")
    return {
        "Betreff": summary,
        "Start(Datum)": start[:8],
        "Start(Zeit)": start[9:15],
        "Ende(Zeit)": end[9:15],
        "AlrmBef": 0x00,
        "Notizen": str(event.get("description") or ""),
        "AlrmRep": 0xC0,
        "Ende(Datum)": end[:8],
        "EndRepeat": "",
        "RepAlln": 0x00,
    }


def remember_binding(
    state_file: Path,
    event: dict,
    record_id: int,
    file_id: int = 0x08,
    ic35_fields: dict | None = None,
) -> None:
    state = load_state(state_file)
    bindings = state.setdefault("bindings", {})
    bindings[event["id"]] = {
        "record_id": int(record_id),
        "file_id": int(file_id),
        "etag": event.get("etag", ""),
        "summary": event.get("summary", ""),
        "updated": event.get("updated", ""),
        "ic35_fields": dict(ic35_fields or {}),
    }
    save_state(state_file, state)



def _ic35_field_value(record: dict, name: str):
    return (record.get("fields") or {}).get(name, {}).get("value", "")


def ic35_schedule_snapshot(record: dict) -> dict:
    """Stable user-data snapshot used only for the controlled reverse-sync test."""
    keys = [
        "Betreff", "Start(Datum)", "Start(Zeit)", "Ende(Zeit)",
        "AlrmBef", "Notizen", "AlrmRep", "Ende(Datum)",
        "EndRepeat", "RepAlln",
    ]
    fields = {}
    for key in keys:
        val = _ic35_field_value(record, key)
        if isinstance(val, (str, int, float, bool)) or val is None:
            fields[key] = val
        else:
            fields[key] = str(val)
    return {
        "record_id": int(record.get("record_id", 0)),
        "file_id": int(record.get("file_id", 0x08)),
        "fields": fields,
    }


def save_ic35_schedule_baseline(state_file: Path, records: list[dict]) -> dict:
    state = load_state(state_file)
    baseline = {}
    for record in records:
        if record.get("deleted"):
            continue
        snap = ic35_schedule_snapshot(record)
        rid = int(snap["record_id"])
        if rid > 0:
            baseline[str(rid)] = snap
    state["ic35_schedule_baseline"] = baseline
    state["ic35_schedule_baseline_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state_file, state)
    return state


def compare_ic35_schedule_baseline(state: dict, records: list[dict]) -> dict:
    """Compare fresh IC35 Schedule data with the saved baseline.

    For the first reverse CREATE test, any deletion or mutation of an old
    baseline record aborts the operation. Exactly one new record is required.
    """
    baseline = dict(state.get("ic35_schedule_baseline") or {})
    current = {}
    for record in records:
        if record.get("deleted"):
            continue
        snap = ic35_schedule_snapshot(record)
        rid = int(snap["record_id"])
        if rid > 0:
            current[str(rid)] = (record, snap)

    base_ids = set(baseline)
    cur_ids = set(current)
    missing = sorted(base_ids - cur_ids, key=int)
    new_ids = sorted(cur_ids - base_ids, key=int)
    changed = []
    for rid in sorted(base_ids & cur_ids, key=int):
        if baseline[rid].get("fields") != current[rid][1].get("fields"):
            changed.append(rid)

    bound_record_ids = {
        str(int(binding.get("record_id", 0)))
        for binding in (state.get("bindings") or {}).values()
        if int(binding.get("record_id", 0) or 0) > 0
    }
    new_unbound = [rid for rid in new_ids if rid not in bound_record_ids]
    new_already_bound = [rid for rid in new_ids if rid in bound_record_ids]

    return {
        "missing_ids": missing,
        "changed_ids": changed,
        "new_ids": new_ids,
        "new_unbound_ids": new_unbound,
        "new_already_bound_ids": new_already_bound,
        "current": current,
    }


def _ic35_compact_datetime(record: dict, date_key: str, time_key: str, label: str) -> datetime:
    date_raw = str(_ic35_field_value(record, date_key) or "")
    time_raw = str(_ic35_field_value(record, time_key) or "")
    date8 = date_raw[:8]
    time4 = time_raw[:4]
    if len(date8) != 8 or not date8.isdigit():
        raise ValueError(f"{label}: ungültiges IC35-Datum {date_raw!r}.")
    if len(time4) != 4 or not time4.isdigit():
        raise ValueError(f"{label}: ungültige IC35-Uhrzeit {time_raw!r}.")
    try:
        dt = datetime.strptime(date8 + time4, "%Y%m%d%H%M")
    except ValueError as exc:
        raise ValueError(f"{label}: ungültiges Datum/Uhrzeit {date8} {time4}.") from exc
    return dt.replace(tzinfo=LOCAL_TZ)


def ic35_schedule_to_google_body(record: dict, calendar_id: str) -> dict:
    """Convert one simple IC35 Schedule record into a timed Google event."""
    rid = int(record.get("record_id", 0))
    if rid <= 0:
        raise ValueError("IC35 Schedule Record-ID fehlt.")

    summary = str(_ic35_field_value(record, "Betreff") or "").strip()
    if not summary:
        raise ValueError("Der IC35-Termin hat keinen Betreff.")

    start = _ic35_compact_datetime(record, "Start(Datum)", "Start(Zeit)", "START")
    end = _ic35_compact_datetime(record, "Ende(Datum)", "Ende(Zeit)", "ENDE")
    if end <= start:
        raise ValueError(
            f"Terminende ({end:%d.%m.%Y %H:%M}) liegt nicht nach dem Beginn "
            f"({start:%d.%m.%Y %H:%M})."
        )

    # First reverse-direction test deliberately supports only the same simple
    # appointments already proven in the Google->IC35 direction.
    try:
        alarm_before = int(_ic35_field_value(record, "AlrmBef") or 0)
        alarm_repeat = int(_ic35_field_value(record, "AlrmRep") or 0)
    except (TypeError, ValueError):
        raise ValueError("Alarm-/Wiederholungsfelder des IC35-Termins sind ungültig.")
    if alarm_before != 0:
        raise ValueError(
            "Der erste IC35→Google-Test unterstützt noch keine IC35-Erinnerung. "
            "Bitte den Testtermin ohne Alarm anlegen."
        )
    if (alarm_repeat & 0x0F) != 0:
        raise ValueError(
            "Der erste IC35→Google-Test unterstützt noch keine Wiederholung. "
            "Bitte einen einmaligen Testtermin anlegen."
        )

    notes = str(_ic35_field_value(record, "Notizen") or "")

    # Google requires custom IDs to contain only base32hex characters.
    # A SHA-1 hex digest is a valid subset (0-9, a-f), while the deterministic
    # seed prevents duplicate creation if Google accepted the insert but the
    # local state write failed afterwards.
    seed = (
        f"{calendar_id}|{rid}|{summary}|"
        f"{start.isoformat()}|{end.isoformat()}|{notes}"
    )
    event_id = "ic35" + hashlib.sha1(seed.encode("utf-8")).hexdigest()

    return {
        "id": event_id,
        "summary": summary,
        "description": notes,
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                "ic35Bridge": "siemens-ic35",
                "ic35RecordId": str(rid),
                "ic35FileId": f"{int(record.get('file_id', 0x08)) & 0xFF:02x}",
            }
        },
    }




def _sync_semantic_from_fields(fields: dict) -> dict:
    """Normalize only the simple Schedule fields proven on the real IC35."""
    def _ival(name, default=0):
        try:
            return int(fields.get(name, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "summary": str(fields.get("Betreff") or "").strip(),
        "start_date": str(fields.get("Start(Datum)") or "")[:8],
        "start_time": str(fields.get("Start(Zeit)") or "")[:4],
        "end_date": str(fields.get("Ende(Datum)") or "")[:8],
        "end_time": str(fields.get("Ende(Zeit)") or "")[:4],
        "notes": str(fields.get("Notizen") or "").strip(),
        "alarm_before": _ival("AlrmBef", 0),
        # The high bits are IC35-internal flags. The low nibble is the repeat mode.
        "repeat_mode": _ival("AlrmRep", 0) & 0x0F,
    }


def ic35_schedule_semantic(record: dict) -> dict:
    return _sync_semantic_from_fields(ic35_schedule_snapshot(record).get("fields", {}))


def google_event_semantic(event: dict) -> dict:
    return _sync_semantic_from_fields(event_to_ic35_fields(event))


def list_current_bound_events(svc, state: dict) -> dict:
    """Return event_id -> {'event': event|None, 'deleted': bool, 'binding': binding}."""
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt.")
    result = {}
    for event_id, binding in (state.get("bindings") or {}).items():
        try:
            ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
            deleted = ev.get("status") == "cancelled"
            result[event_id] = {
                "event": None if deleted else ev,
                "deleted": deleted,
                "binding": binding,
            }
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                result[event_id] = {
                    "event": None,
                    "deleted": True,
                    "binding": binding,
                }
            else:
                raise
    return result



def _ic35_record_start_dates(records: list[dict]) -> list[datetime]:
    dates = []
    for record in records:
        try:
            raw = str(_ic35_field_value(record, "Start(Datum)") or "")[:8]
            if len(raw) == 8 and raw.isdigit():
                dates.append(datetime.strptime(raw, "%Y%m%d").replace(tzinfo=LOCAL_TZ))
        except Exception:
            pass
    return dates


def list_google_events_for_reconciliation(
    svc,
    state: dict,
    records: list[dict],
    future_days: int = 370,
) -> tuple[list[dict], dict]:
    """Return the full Google event inventory relevant to the IC35.

    Window start:
      earliest current IC35 Schedule date, otherwise 370 days ago.
    Window end:
      today + future_days.

    This deliberately performs a full paginated list with singleEvents=True.
    Existing recurring series therefore appear as concrete instances that can
    be mirrored as individual IC35 appointments.
    """
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt.")

    now_local = datetime.now(LOCAL_TZ)
    ic35_dates = _ic35_record_start_dates(records)
    if ic35_dates:
        start_local = min(ic35_dates) - timedelta(days=2)
    else:
        start_local = now_local - timedelta(days=370)
    end_local = now_local + timedelta(days=int(future_days))

    time_min = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    time_max = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    items = []
    page_token = None
    while True:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            showDeleted=False,
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Cancelled events are excluded by showDeleted=False, but keep this guard.
    items = [ev for ev in items if ev.get("status") != "cancelled"]
    return items, {
        "time_min": time_min,
        "time_max": time_max,
        "count": len(items),
    }


def build_calendar_two_way_plan(svc, state: dict, records: list[dict]) -> dict:
    """Build a conservative two-way plan for simple appointments.

    The planner uses:
    - IC35 baseline fields
    - Google ETags
    - durable Google event <-> IC35 Record-ID bindings
    - the existing Google "created after baseline" detection for unbound events.

    Unsupported alarm/recurrence events are skipped rather than guessed.
    """
    baseline = dict(state.get("ic35_schedule_baseline") or {})
    bindings = dict(state.get("bindings") or {})

    current = {}
    for record in records:
        if record.get("deleted"):
            continue
        snap = ic35_schedule_snapshot(record)
        rid = int(snap.get("record_id") or 0)
        if rid > 0:
            current[str(rid)] = {"record": record, "snapshot": snap}

    bound_by_rid = {}
    for event_id, binding in bindings.items():
        try:
            rid = int(binding.get("record_id") or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid > 0:
            bound_by_rid[str(rid)] = (event_id, binding)

    bound_events = list_current_bound_events(svc, state)

    plan = {
        "google_to_ic35_create": [],
        "google_to_ic35_update": [],
        "google_to_ic35_delete": [],
        "ic35_to_google_create": [],
        "ic35_to_google_update": [],
        "ic35_to_google_delete": [],
        "bind_pairs": [],
        "converged": [],
        "cleanup_bindings": [],
        "unchanged": [],
        "conflicts": [],
        "skipped": [],
    }

    # ----- Existing durable bindings -----
    for event_id, binding in bindings.items():
        rid = str(int(binding.get("record_id") or 0))
        if rid == "0":
            plan["conflicts"].append({
                "type": "invalid-binding",
                "event_id": event_id,
                "reason": "Google-Bindung besitzt keine gültige IC35 Record-ID.",
            })
            continue

        cur = current.get(rid)
        ginfo = bound_events.get(event_id, {"event": None, "deleted": True, "binding": binding})
        gev = ginfo.get("event")
        google_deleted = bool(ginfo.get("deleted"))

        base_fields = dict(binding.get("ic35_fields") or {})
        if not base_fields:
            base_fields = dict((baseline.get(rid) or {}).get("fields") or {})

        if cur:
            ic_sem = ic35_schedule_semantic(cur["record"])
        else:
            ic_sem = None

        base_sem = _sync_semantic_from_fields(base_fields) if base_fields else None

        google_changed = False
        google_sem = None
        if gev is not None:
            old_etag = str(binding.get("etag") or "")
            new_etag = str(gev.get("etag") or "")
            google_changed = bool(old_etag and new_etag and old_etag != new_etag)
            if not old_etag:
                google_changed = str(binding.get("updated") or "") != str(gev.get("updated") or "")
            if google_changed:
                try:
                    google_sem = google_event_semantic(gev)
                except Exception as exc:
                    plan["conflicts"].append({
                        "type": "google-unsupported",
                        "record_id": int(rid),
                        "event_id": event_id,
                        "reason": str(exc),
                    })
                    continue

        ic_changed = False
        if cur is not None:
            if base_sem is None:
                # No trustworthy common baseline: do not guess.
                ic_changed = False
            else:
                ic_changed = ic_sem != base_sem

        # Both sides still exist.
        if cur is not None and not google_deleted:
            if base_sem is None:
                # Recover a missing baseline only when Google hasn't changed.
                if google_changed:
                    plan["conflicts"].append({
                        "type": "missing-baseline",
                        "record_id": int(rid),
                        "event_id": event_id,
                        "reason": "Bindung ohne gemeinsame Baseline und Google wurde geändert.",
                    })
                else:
                    plan["converged"].append({
                        "record_id": int(rid), "event_id": event_id,
                        "event": gev, "record": cur["record"],
                    })
                continue

            if not ic_changed and not google_changed:
                plan["unchanged"].append({
                    "record_id": int(rid), "event_id": event_id,
                    "event": gev, "record": cur["record"],
                })
            elif ic_changed and not google_changed:
                try:
                    ic35_schedule_to_google_patch(cur["record"])
                    plan["ic35_to_google_update"].append({
                        "record_id": int(rid), "event_id": event_id,
                        "binding": binding, "record": cur["record"],
                    })
                except Exception as exc:
                    plan["skipped"].append({
                        "type": "ic35-update-unsupported",
                        "record_id": int(rid), "reason": str(exc),
                    })
            elif google_changed and not ic_changed:
                try:
                    fields = event_to_ic35_fields(gev)
                    plan["google_to_ic35_update"].append({
                        "record_id": int(rid), "event_id": event_id,
                        "binding": binding, "event": gev,
                        "fields": fields, "baseline_semantic": base_sem,
                    })
                except Exception as exc:
                    plan["skipped"].append({
                        "type": "google-update-unsupported",
                        "event_id": event_id, "reason": str(exc),
                    })
            else:
                # Both changed: only harmless if both ended up semantically equal.
                if google_sem is None:
                    try:
                        google_sem = google_event_semantic(gev)
                    except Exception as exc:
                        plan["conflicts"].append({
                            "type": "both-changed-unsupported",
                            "record_id": int(rid), "event_id": event_id,
                            "reason": str(exc),
                        })
                        continue
                if ic_sem == google_sem:
                    plan["converged"].append({
                        "record_id": int(rid), "event_id": event_id,
                        "event": gev, "record": cur["record"],
                    })
                else:
                    plan["conflicts"].append({
                        "type": "both-changed",
                        "record_id": int(rid), "event_id": event_id,
                        "reason": "Termin wurde auf IC35 und Google unterschiedlich geändert.",
                    })
            continue

        # IC35 deleted, Google still exists.
        if cur is None and not google_deleted:
            if google_changed:
                plan["conflicts"].append({
                    "type": "ic35-deleted-google-changed",
                    "record_id": int(rid), "event_id": event_id,
                    "reason": "Auf IC35 gelöscht, aber Google-Termin parallel geändert.",
                })
            else:
                plan["ic35_to_google_delete"].append({
                    "record_id": int(rid), "event_id": event_id, "binding": binding,
                })
            continue

        # Google deleted, IC35 still exists.
        if cur is not None and google_deleted:
            if ic_changed:
                plan["conflicts"].append({
                    "type": "google-deleted-ic35-changed",
                    "record_id": int(rid), "event_id": event_id,
                    "reason": "Bei Google gelöscht, aber IC35-Termin parallel geändert.",
                })
            else:
                plan["google_to_ic35_delete"].append({
                    "record_id": int(rid), "event_id": event_id,
                    "binding": binding, "baseline_semantic": base_sem,
                })
            continue

        # Both gone: stale binding can simply disappear.
        if cur is None and google_deleted:
            plan["cleanup_bindings"].append({"record_id": int(rid), "event_id": event_id})

    # ----- Full reconciliation of unbound inventory on both sides -----
    #
    # v3.0.0 only looked at Google events CREATED after baseline_utc. That was
    # appropriate during the safe test phase but omitted all appointments that
    # already existed in the selected calendar before IC35 sync was configured. The final
    # reconciliation lists the complete relevant time window instead.
    google_inventory, google_window = list_google_events_for_reconciliation(
        svc, state, records
    )
    plan["google_window"] = google_window

    bound_event_ids = set(bindings)
    new_google = []
    for ev in google_inventory:
        if ev.get("id") in bound_event_ids:
            continue
        try:
            fields = event_to_ic35_fields(ev)
            new_google.append({
                "event": ev,
                "fields": fields,
                "semantic": _sync_semantic_from_fields(fields),
            })
        except Exception as exc:
            plan["skipped"].append({
                "type": "existing-google-unsupported",
                "event_id": ev.get("id", ""),
                "summary": ev.get("summary", ""),
                "reason": str(exc),
            })

    # Every unbound simple IC35 appointment participates in initial
    # reconciliation, including records that predate the v2.x baseline.
    # Exact semantic matches are paired below, so this does not duplicate
    # appointments that already exist on both sides.
    bound_ids = set(bound_by_rid)
    new_ic35 = []
    for rid, info in current.items():
        if rid in bound_ids:
            continue
        try:
            ic35_schedule_to_google_body(info["record"], state.get("calendar_id", ""))
            new_ic35.append({
                "record_id": int(rid),
                "record": info["record"],
                "semantic": ic35_schedule_semantic(info["record"]),
            })
        except Exception as exc:
            plan["skipped"].append({
                "type": "existing-ic35-unsupported",
                "record_id": int(rid),
                "reason": str(exc),
            })

    # If the user independently created exactly the same appointment on both sides,
    # bind it instead of duplicating it.
    used_g = set()
    used_i = set()
    for gi, gitem in enumerate(new_google):
        candidates = [
            ii for ii, iitem in enumerate(new_ic35)
            if ii not in used_i and iitem["semantic"] == gitem["semantic"]
        ]
        if len(candidates) == 1:
            ii = candidates[0]
            # Ensure the IC35 record has no competing identical Google candidate.
            competing = [
                x for x, other in enumerate(new_google)
                if x not in used_g and other["semantic"] == new_ic35[ii]["semantic"]
            ]
            if len(competing) == 1:
                used_g.add(gi)
                used_i.add(ii)
                plan["bind_pairs"].append({
                    "event": gitem["event"],
                    "record": new_ic35[ii]["record"],
                    "record_id": new_ic35[ii]["record_id"],
                })

    for gi, gitem in enumerate(new_google):
        if gi not in used_g:
            plan["google_to_ic35_create"].append(gitem)
    for ii, iitem in enumerate(new_ic35):
        if ii not in used_i:
            plan["ic35_to_google_create"].append(iitem)

    return plan


def summarize_calendar_plan(plan: dict) -> dict:
    return {
        "google_to_ic35_create": len(plan.get("google_to_ic35_create", [])),
        "google_to_ic35_update": len(plan.get("google_to_ic35_update", [])),
        "google_to_ic35_delete": len(plan.get("google_to_ic35_delete", [])),
        "ic35_to_google_create": len(plan.get("ic35_to_google_create", [])),
        "ic35_to_google_update": len(plan.get("ic35_to_google_update", [])),
        "ic35_to_google_delete": len(plan.get("ic35_to_google_delete", [])),
        "bound_without_copy": len(plan.get("bind_pairs", [])),
        "conflicts": len(plan.get("conflicts", [])),
        "skipped": len(plan.get("skipped", [])),
        "google_inventory": int((plan.get("google_window") or {}).get("count", 0)),
    }


def find_binding_for_record_id(state: dict, record_id: int) -> tuple[str, dict] | None:
    rid = int(record_id)
    for event_id, binding in (state.get("bindings") or {}).items():
        try:
            bound_rid = int(binding.get("record_id") or 0)
        except (TypeError, ValueError):
            continue
        if bound_rid == rid:
            return event_id, binding
    return None


def ic35_schedule_to_google_patch(record: dict) -> dict:
    """Convert the writable IC35 fields to a Google events.patch body.

    Fields not represented by the IC35 are intentionally omitted so Google-only
    data such as attendees, location, color and attachments survive.
    """
    summary = str(_ic35_field_value(record, "Betreff") or "").strip()
    if not summary:
        raise ValueError("Der IC35-Termin hat keinen Betreff.")

    start = _ic35_compact_datetime(record, "Start(Datum)", "Start(Zeit)", "START")
    end = _ic35_compact_datetime(record, "Ende(Datum)", "Ende(Zeit)", "ENDE")
    if end <= start:
        raise ValueError(
            f"Terminende ({end:%d.%m.%Y %H:%M}) liegt nicht nach dem Beginn "
            f"({start:%d.%m.%Y %H:%M})."
        )

    try:
        alarm_before = int(_ic35_field_value(record, "AlrmBef") or 0)
        alarm_repeat = int(_ic35_field_value(record, "AlrmRep") or 0)
    except (TypeError, ValueError):
        raise ValueError("Alarm-/Wiederholungsfelder des IC35-Termins sind ungültig.")

    if alarm_before != 0:
        raise ValueError(
            "Der kontrollierte IC35→Google UPDATE-Test unterstützt noch keine "
            "IC35-Erinnerung. Bitte den Testtermin ohne Alarm verwenden."
        )
    if (alarm_repeat & 0x0F) != 0:
        raise ValueError(
            "Der kontrollierte IC35→Google UPDATE-Test unterstützt noch keine "
            "Wiederholung. Bitte einen einmaligen Testtermin verwenden."
        )

    notes = str(_ic35_field_value(record, "Notizen") or "")

    return {
        "summary": summary,
        "description": notes,
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "reminders": {"useDefault": False},
    }



def prepare_google_event_delete_for_ic35(
    svc,
    state: dict,
    event_id: str,
    binding: dict,
    record_id: int,
) -> tuple[dict, object, str]:
    """Verify a bound Google event and prepare, but do not execute, DELETE.

    Returns (current_event, delete_request, expected_etag). The caller can
    persist a recovery JSON before executing the destructive request.
    """
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt im gespeicherten Zustand.")

    try:
        current = svc.events().get(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()
    except HttpError as exc:
        if getattr(exc, "resp", None) is not None and exc.resp.status in (404, 410):
            raise RuntimeError(
                "Konflikt: Der verknüpfte Google-Termin existiert bereits nicht mehr. "
                "Die App entfernt die lokale Bindung deshalb nicht automatisch."
            ) from exc
        raise

    if current.get("status") == "cancelled":
        raise RuntimeError(
            "Konflikt: Der verknüpfte Google-Termin ist bei Google bereits gelöscht/cancelled."
        )

    private = ((current.get("extendedProperties") or {}).get("private") or {})
    bound_rid = str(int(record_id))
    google_rid = str(private.get("ic35RecordId") or "")
    if google_rid and google_rid != bound_rid:
        raise RuntimeError(
            f"Sicherheitsabbruch: Google-Event trägt ic35RecordId={google_rid!r}, "
            f"erwartet war {bound_rid!r}."
        )

    expected_etag = str(binding.get("etag") or "")
    current_etag = str(current.get("etag") or "")
    if expected_etag and current_etag and expected_etag != current_etag:
        raise RuntimeError(
            "Konflikt: Der Google-Termin wurde seit dem letzten erfolgreichen "
            "Sync ebenfalls geändert. IC35-Löschung wird NICHT auf Google übertragen."
        )

    request = svc.events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates="none",
    )
    if expected_etag:
        request.headers["If-Match"] = expected_etag

    return current, request, expected_etag


def execute_prepared_google_delete(request) -> None:
    """Execute a previously verified Google DELETE request."""
    try:
        request.execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 412:
            raise RuntimeError(
                "Konflikt: Google meldet 'Precondition Failed' (ETag geändert). "
                "Der Termin wurde parallel geändert; er wurde NICHT gelöscht."
            ) from exc
        if status in (404, 410):
            raise RuntimeError(
                "Konflikt: Der Google-Termin ist beim Löschversuch bereits verschwunden."
            ) from exc
        raise


def patch_google_event_from_ic35(
    svc,
    state: dict,
    event_id: str,
    binding: dict,
    record: dict,
) -> dict:
    """Patch a bound Google event only if Google's ETag is unchanged."""
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt im gespeicherten Zustand.")

    current = svc.events().get(
        calendarId=calendar_id,
        eventId=event_id,
    ).execute()

    if current.get("status") == "cancelled":
        raise RuntimeError(
            "Konflikt: Der verknüpfte Google-Termin wurde bereits gelöscht."
        )

    expected_etag = str(binding.get("etag") or "")
    current_etag = str(current.get("etag") or "")
    if expected_etag and current_etag and expected_etag != current_etag:
        raise RuntimeError(
            "Konflikt: Der Google-Termin wurde seit dem letzten erfolgreichen "
            "Sync ebenfalls geändert. IC35-Änderung wird NICHT darübergeschrieben."
        )

    body = ic35_schedule_to_google_patch(record)
    request = svc.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=body,
        sendUpdates="none",
    )

    # Protect the tiny race between GET and PATCH as well. googleapiclient's
    # HttpRequest exposes HTTP headers before execute().
    if expected_etag:
        request.headers["If-Match"] = expected_etag

    try:
        return request.execute()
    except HttpError as exc:
        if getattr(exc, "resp", None) is not None and exc.resp.status == 412:
            raise RuntimeError(
                "Konflikt: Google meldet 'Precondition Failed' (ETag geändert). "
                "Der Termin wurde parallel geändert; es wurde nichts überschrieben."
            ) from exc
        raise


def create_google_event_from_ic35(svc, state: dict, record: dict) -> tuple[dict, bool]:
    """Insert one IC35 event. Returns (event, was_newly_created).

    HTTP 409 is handled idempotently: if the deterministic event ID already
    exists and carries the same IC35 Record-ID, it is treated as the previous
    successful insert rather than creating a duplicate.
    """
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt im gespeicherten Zustand.")

    body = ic35_schedule_to_google_body(record, calendar_id)
    event_id = body["id"]
    rid = str(int(record.get("record_id", 0)))

    try:
        event = svc.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates="none",
        ).execute()
        return event, True
    except HttpError as exc:
        if getattr(exc, "resp", None) is not None and exc.resp.status == 409:
            existing = svc.events().get(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            private = ((existing.get("extendedProperties") or {}).get("private") or {})
            if private.get("ic35RecordId") == rid:
                return existing, False
        raise


def forget_binding(state_file: Path, event_id: str) -> None:
    state = load_state(state_file)
    bindings = state.setdefault("bindings", {})
    bindings.pop(str(event_id), None)
    save_state(state_file, state)


def list_bound_event_changes(svc, state: dict) -> tuple[list[tuple[dict, dict]], list[tuple[str, dict]]]:
    """Return (changed_active_events, deleted_events) for bound Google events."""
    calendar_id = state.get("calendar_id")
    if not calendar_id:
        raise RuntimeError("Google-Kalender-ID fehlt im gespeicherten Zustand.")

    changed = []
    deleted = []
    for event_id, binding in (state.get("bindings") or {}).items():
        try:
            ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status in (404, 410):
                deleted.append((event_id, binding))
                continue
            raise

        if ev.get("status") == "cancelled":
            deleted.append((event_id, binding))
            continue

        old_etag = str(binding.get("etag") or "")
        new_etag = str(ev.get("etag") or "")
        old_updated = str(binding.get("updated") or "")
        new_updated = str(ev.get("updated") or "")
        if (old_etag and new_etag and old_etag != new_etag) or (
            not (old_etag and new_etag) and old_updated != new_updated
        ):
            changed.append((ev, binding))

    return changed, deleted
