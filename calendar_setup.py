"""Calendar selection and per-target state, without a personal calendar name."""
import hashlib
from pathlib import Path
from memo_sync import read_json, atomic_json


def writable_calendars(service):
    result, token, seen = [], None, set()
    while True:
        response = service.calendarList().list(maxResults=250, pageToken=token).execute()
        result.extend(c for c in response.get('items', [])
                      if not c.get('deleted') and c.get('accessRole') in ('owner', 'writer'))
        token = response.get('nextPageToken')
        if not token:
            return result
        if token in seen:
            raise RuntimeError('Kalenderliste konnte nicht vollständig gelesen werden.')
        seen.add(token)


def state_path(data_dir, calendar_id):
    digest = hashlib.sha256(calendar_id.encode('utf-8')).hexdigest()
    return Path(data_dir) / 'calendars' / digest / 'state.json'


def selected_state_path(data_dir):
    selected = read_json(Path(data_dir) / 'calendar_selection.json', {})
    if selected.get('id'):
        return state_path(data_dir, selected['id'])
    return Path(data_dir) / 'calendar_unconfigured.json'


def select(data_dir, calendar):
    path = state_path(data_dir, calendar['id'])
    state = read_json(path, {'calendar_id': calendar['id'], 'bindings': {}})
    if state.get('calendar_id') != calendar['id']:
        raise ValueError('Kalender-State passt nicht zum gewählten Ziel.')
    state['calendar_name'] = calendar.get('summary', '')
    atomic_json(path, state)
    atomic_json(Path(data_dir) / 'calendar_selection.json', {'id': calendar['id']})
    return path
