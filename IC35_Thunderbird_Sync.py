# GitHub preparation based on the hardware-tested v3.3.0a1 client.
from __future__ import annotations
import importlib.util
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
from serial.tools import list_ports
import ic35_protocol as proto
import manager_protocol as mgr
import google_calendar_bridge as gcal
import memo_sync
import sync_sounds
import google_tasks_bridge as gtasks
import direct_tasks_sync
import calendar_setup
from bridge import ensure_radicale_storage, fetch_storage_snapshot, contact_semantic_to_ic35_fields, mark_address_resource_ic35_id_dav, remember_address_binding, publish_contacts_dav_nolist, analyze_contact_two_way, contact_record_to_semantic, delete_address_resource_dav
APP_VERSION = '3.3.0a1'
APP_NAME = 'Siemens IC35 Sync'
HOST = '127.0.0.1'
RADICALE_PORT = 5232
RADICALE_USER = 'ic35'
APPDATA = Path(os.environ.get('IC35_SYNC_DATA_DIR') or (Path(os.environ.get('APPDATA', Path.home())) / 'IC35SyncPreview'))
STORAGE = APPDATA / 'radicale'
EXPORTS = APPDATA / 'exports'
LOGS = APPDATA / 'logs'
BACKUPS = APPDATA / 'backups'
REPORTS = APPDATA / 'reports'
STATE = APPDATA / 'sync_state_v0.9.1.json'
GOOGLE_CREDENTIALS = APPDATA / 'google_credentials.json'
GOOGLE_TOKEN = APPDATA / 'google_token.json'
GOOGLE_STATE = calendar_setup.selected_state_path(APPDATA)
for p in (APPDATA, STORAGE, EXPORTS, LOGS, BACKUPS, REPORTS):
    p.mkdir(parents=True, exist_ok=True)

def read_tasks_config():
    config = memo_sync.read_json(APPDATA / 'google_tasks_settings.json', {'enabled': False})
    config['enabled'] = config.get('include_in_all', bool(config.get('list_id')))
    return config

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME} v{APP_VERSION}')
        self.geometry('980x760')
        self.minsize(820, 640)
        self.radicale_proc = None
        self.msg_queue = queue.Queue()
        self.work_thread = None
        self.last_counts = {'contacts': 0, 'calendar': 0, 'memos': 0}
        self._build_ui()
        self.refresh_ports()
        self._update_last_backup_label()
        self.after(100, self._drain_queue)
        self.after(1000, self._poll_radicale)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

    def _build_ui(self):
        self.geometry('900x760')
        self.minsize(760, 650)
        style = ttk.Style(self)
        style.configure('BigSync.TButton', font=('Segoe UI', 15, 'bold'), padding=(22, 14))
        style.configure('Phase.TLabel', font=('Segoe UI', 11, 'bold'))
        top = ttk.Frame(self, padding=(18, 16, 18, 6))
        top.pack(fill='x')
        ttk.Label(top, text='Siemens IC35 Sync', font=('Segoe UI', 21, 'bold')).pack(anchor='center')
        ttk.Label(top, text='Kontakte ↔ Thunderbird/CardDAV   ·   Kalender ↔ Google Kalender', foreground='#555').pack(anchor='center', pady=(4, 0))
        sync_panel = ttk.Frame(self, padding=(18, 6, 18, 12))
        sync_panel.pack(fill='x')
        self.sync_canvas = tk.Canvas(sync_panel, width=104, height=104, highlightthickness=0, bg=self.cget('bg'))
        self.sync_canvas.pack(pady=(2, 0))
        self._spinner_running = False
        self._spinner_angle = 0.0
        self._spinner_center = (52.0, 52.0)
        self._spinner_radius = 34.0
        self._spinner_arc_degrees = 160.0
        self.spinner_top = self.sync_canvas.create_line(0, 0, 1, 1, fill='#3b6ea8', width=6, capstyle=tk.ROUND, joinstyle=tk.ROUND, arrow=tk.LAST, arrowshape=(12, 14, 6))
        self.spinner_bottom = self.sync_canvas.create_line(0, 0, 1, 1, fill='#777777', width=6, capstyle=tk.ROUND, joinstyle=tk.ROUND, arrow=tk.LAST, arrowshape=(12, 14, 6))
        self._draw_spinner_frame(0.0)
        self.sync_all_btn = ttk.Button(sync_panel, text='⟳  ALLES SYNCHRONISIEREN  ⟳', style='BigSync.TButton', command=self.start_full_sync)
        self.sync_all_btn.pack(pady=(2, 8))
        self.stage_var = tk.StringVar(value='Bereit')
        ttk.Label(sync_panel, textvariable=self.stage_var, style='Phase.TLabel').pack()
        ttk.Label(sync_panel, text='Ein Klick in der App · SyncStation einmal drücken · Vollbackup bei Bedarf über Nur Backup', foreground='#666').pack(pady=(3, 0))
        status = ttk.LabelFrame(self, text='Status', padding=10)
        status.pack(fill='x', padx=18, pady=(0, 10))
        self.device_var = tk.StringVar(value='Noch nicht gelesen')
        self.rad_status_var = tk.StringVar(value='Radicale: prüfe …')
        self.counts_var = tk.StringVar(value='Kontakte: 0 · Kalender/Aufgaben: 0 · Memos: 0')
        self.mode_var = tk.StringVar(value='Vorabversion · Kontakte, Kalender, Notizen und Aufgaben')
        self.backup_var = tk.StringVar(value='Letztes Komplettbackup: noch keines')
        ttk.Label(status, text='IC35:', font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(status, textvariable=self.device_var).grid(row=0, column=1, sticky='w', padx=(6, 20))
        ttk.Label(status, textvariable=self.rad_status_var).grid(row=0, column=2, sticky='w')
        ttk.Label(status, textvariable=self.counts_var).grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Label(status, textvariable=self.backup_var).grid(row=2, column=0, columnspan=3, sticky='w', pady=(3, 0))
        ttk.Label(status, textvariable=self.mode_var).grid(row=3, column=0, columnspan=3, sticky='w', pady=(3, 0))
        status.columnconfigure(1, weight=1)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(status, variable=self.progress_var, maximum=27)
        self.progress.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(8, 0))
        tools = ttk.LabelFrame(self, text='Einstellungen & Werkzeuge', padding=10)
        tools.pack(fill='x', padx=18, pady=(0, 10))
        row1 = ttk.Frame(tools)
        row1.pack(fill='x')
        ttk.Label(row1, text='COM-Port:').pack(side='left')
        self.port_var = tk.StringVar(value='')
        self.port_combo = ttk.Combobox(row1, textvariable=self.port_var, width=12, state='readonly')
        self.port_combo.pack(side='left', padx=(6, 6))
        self.refresh_btn = ttk.Button(row1, text='Ports aktualisieren', command=self.refresh_ports)
        self.refresh_btn.pack(side='left')
        self.google_connect_btn = ttk.Button(row1, text='Google verbinden', command=self.connect_google_calendar)
        self.google_connect_btn.pack(side='left', padx=(8, 0))
        self.backup_btn = ttk.Button(row1, text='Nur Backup', command=self.start_backup)
        self.backup_btn.pack(side='left', padx=(8, 0))
        self.data_btn = ttk.Button(row1, text='Datenordner', command=self.open_data_folder)
        self.data_btn.pack(side='left', padx=(8, 0))
        self.notes_btn = ttk.Button(tools, text='Notiz-Ziel einstellen …', command=self.configure_notes)
        self.notes_btn.pack(anchor='w', pady=(8, 0))
        try:
            notes_mode = self._notes_config().get('mode', 'Aus')
        except Exception:
            notes_mode = 'Einstellungsdatei nicht lesbar'
        self.notes_status = tk.StringVar(value='Notizen: ' + notes_mode)
        ttk.Label(tools, textvariable=self.notes_status).pack(anchor='w')
        self.tasks_btn = ttk.Button(tools, text='Aufgaben-Ziel (Google Tasks) …', command=self.configure_tasks)
        self.tasks_btn.pack(anchor='w', pady=(6, 0))
        log_frame = ttk.LabelFrame(self, text='Protokoll', padding=8)
        log_frame.pack(fill='both', expand=True, padx=18, pady=(0, 16))
        self.log_text = tk.Text(log_frame, wrap='word', height=20, font=('Consolas', 9))
        scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

    def _spinner_arc_points(self, start_deg: float) -> list[float]:
        """Return sampled points for one clockwise half-circle arrow.

        Canvas Y coordinates grow downward, so increasing the mathematical angle
        produces clockwise visual motion on screen.
        """
        cx, cy = self._spinner_center
        radius = self._spinner_radius
        extent = self._spinner_arc_degrees
        segments = 28
        coords = []
        for i in range(segments + 1):
            angle = math.radians(start_deg + extent * (i / segments))
            coords.extend([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
        return coords

    def _draw_spinner_frame(self, rotation_deg: float):
        top_start = 190.0 + rotation_deg
        bottom_start = 10.0 + rotation_deg
        self.sync_canvas.coords(self.spinner_top, *self._spinner_arc_points(top_start))
        self.sync_canvas.coords(self.spinner_bottom, *self._spinner_arc_points(bottom_start))

    def _start_spinner(self):
        if self._spinner_running:
            return
        self._spinner_running = True
        self._animate_spinner()

    def _stop_spinner(self):
        self._spinner_running = False
        self._spinner_angle = 0.0
        try:
            self._draw_spinner_frame(0.0)
        except tk.TclError:
            pass

    def _animate_spinner(self):
        if not self._spinner_running:
            return
        self._spinner_angle = (self._spinner_angle + 6.0) % 360.0
        try:
            self._draw_spinner_frame(self._spinner_angle)
        except tk.TclError:
            return
        self.after(45, self._animate_spinner)

    def _set_stage(self, text: str):
        self.stage_var.set(str(text))

    def log(self, msg=''):
        self.msg_queue.put(('log', str(msg)))

    def _set_busy(self, busy: bool):
        state = 'disabled' if busy else 'normal'
        for name in ('sync_all_btn', 'backup_btn', 'google_connect_btn', 'refresh_btn', 'data_btn', 'port_combo', 'notes_btn', 'tasks_btn'):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.config(state=state)
                except tk.TclError:
                    pass
        if busy:
            self.progress_var.set(0)
            self._start_spinner()
        else:
            self._stop_spinner()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == 'log':
                    stamp = datetime.now().strftime('%H:%M:%S')
                    self.log_text.insert('end', f'[{stamp}] {payload}\n')
                    self.log_text.see('end')
                elif kind == 'stage':
                    self._set_stage(payload)
                elif kind == 'sound':
                    sync_sounds.play(payload)
                elif kind == 'direct_tasks_done':
                    self._set_busy(False)
                    self.progress_var.set(27)
                    self._update_last_backup_label()
                    self.device_var.set(payload['device'])
                    self._set_stage(f"✓ {payload['count']} Aufgaben auf dem IC35")
                    sync_sounds.play('complete')
                    stats = payload['stats']
                    messagebox.showinfo('Aufgaben synchronisiert', f"Jetzt sind {payload['count']} Aufgaben auf dem IC35.\n\nGoogle → IC35 angelegt/aktualisiert: {stats['write_device']}\nIC35 → Google angelegt/aktualisiert: {stats['write_google']}\nGelöscht: {stats['delete_device'] + stats['delete_google']}\nÜbersprungen: {stats['skipped']}\n\nBericht: {payload['report']}")
                elif kind == 'full_sync_done':
                    self._set_busy(False)
                    self.progress_var.set(27)
                    self._update_last_backup_label()
                    self.device_var.set(payload.get('device', self.device_var.get()))
                    counts = payload.get('counts') or {}
                    self.last_counts = counts
                    self.counts_var.set(f"Kontakte: {counts.get('contacts', 0)} · Kalender/Aufgaben: {counts.get('calendar', 0)} · Memos: {counts.get('memos', 0)}")
                    self._set_stage('✓ Alles synchronisiert')
                    sync_sounds.play('complete')
                    c = payload.get('contacts', {})
                    k = payload.get('calendar', {})
                    t = payload.get('tasks', {})
                    messagebox.showinfo('Synchronisation abgeschlossen', 'Kontakte, Kalender und aktivierte Notizen/Aufgaben wurden synchronisiert.\n\n' + (f"Aufgaben: {t.get('write_device', 0)} auf IC35 und {t.get('write_google', 0)} bei Google angelegt/aktualisiert; {t.get('delete_device', 0) + t.get('delete_google', 0)} gelöscht.\nÜbersprungen: {t.get('skipped', 0)} (Details im Bericht).\n\n" if t.get('enabled') else '') + f"Kontakte:\n  → IC35 neu/aktualisiert/gelöscht: {c.get('created', 0)}/{c.get('updated', 0)}/{c.get('deleted_ic35', 0)}\n  → Thunderbird gelöscht: {c.get('deleted_carddav', 0)}\n\nKalender:\n  Google → IC35 C/U/D: {k.get('g_create', 0)}/{k.get('g_update', 0)}/{k.get('g_delete', 0)}\n  IC35 → Google C/U/D: {k.get('i_create', 0)}/{k.get('i_update', 0)}/{k.get('i_delete', 0)}\n  Konflikte: {k.get('conflicts', 0)} · übersprungen: {k.get('skipped', 0)}\n\nThunderbird aktualisiert CardDAV/Google normalerweise automatisch; bei Bedarf dort einmal manuell synchronisieren.")
                elif kind == 'progress':
                    self.progress_var.set(payload)
                elif kind == 'backup_done':
                    self._set_busy(False)
                    self.progress_var.set(27)
                    self._update_last_backup_label()
                    messagebox.showinfo('IC35-Komplettbackup erstellt', f"Backup erfolgreich gespeichert:\n\n{payload['path']}\n\nGröße: {payload['size']} Byte")
                elif kind == 'error':
                    self._set_busy(False)
                    self.progress_var.set(0)
                    self._set_stage('Fehler – nichts weiter ausführen')
                    messagebox.showerror('Fehler', payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.port_combo['values'] = ports
        if self.port_var.get() not in ports and ports:
            self.port_var.set(ports[0])

    def _selected_port(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror('COM-Port', 'Bitte einen COM-Port auswählen.')
            return None
        return port

    def _start_worker(self, target, *args):
        """Start one background worker and forward all positional arguments.

        Most older workers receive only `port`. The IC35→Google baseline/create
        worker additionally receives `baseline_only`, so the old fixed
        `(port,)` forwarding prevented the thread from starting at all.
        """
        if self.work_thread and self.work_thread.is_alive():
            return
        self._set_busy(True)
        self.log_text.delete('1.0', 'end')
        self.work_thread = threading.Thread(target=target, args=tuple(args), daemon=True)
        self.work_thread.start()

    def radicale_installed(self):
        return importlib.util.find_spec('radicale') is not None

    def radicale_running(self):
        try:
            with socket.create_connection((HOST, RADICALE_PORT), timeout=0.2):
                return True
        except OSError:
            return False

    def _poll_radicale(self):
        installed = self.radicale_installed()
        running = self.radicale_running()
        if not installed:
            self.rad_status_var.set('Radicale: nicht installiert (install.bat ausführen)')
        elif running:
            self.rad_status_var.set(f'Radicale: läuft auf http://{HOST}:{RADICALE_PORT}')
        else:
            self.rad_status_var.set('Radicale: installiert, aber gestoppt')
        self.after(1000, self._poll_radicale)

    def start_radicale(self):
        if not self.radicale_installed():
            raise RuntimeError('Radicale ist nicht installiert. Bitte zuerst install.bat ausführen.')
        if self.radicale_running():
            self.log('Radicale läuft bereits.')
            return
        ensure_radicale_storage(STORAGE)
        LOGS.mkdir(parents=True, exist_ok=True)
        radicale_log = LOGS / 'radicale_start.log'
        launcher = Path(__file__).with_name('radicale_windows_launcher.py')
        cmd = [sys.executable, str(launcher), '--config', '', '--storage-filesystem-folder', str(STORAGE), '--storage-type', 'multifilesystem_nolock', '--auth-type', 'none', '--server-hosts', f'{HOST}:{RADICALE_PORT}']
        self.log('Starte Radicale …')
        self.log(f'Radicale-Log: {radicale_log}')
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self._radicale_log_handle = open(radicale_log, 'w', encoding='utf-8')
        self.radicale_proc = subprocess.Popen(cmd, stdout=self._radicale_log_handle, stderr=subprocess.STDOUT, creationflags=creationflags)
        for _ in range(120):
            if self.radicale_running():
                self.log('Radicale gestartet.')
                return
            if self.radicale_proc.poll() is not None:
                break
            time.sleep(0.1)
        try:
            self._radicale_log_handle.flush()
        except Exception:
            pass
        details = ''
        try:
            text = radicale_log.read_text(encoding='utf-8', errors='replace').strip()
            if text:
                details = '\n\nRadicale meldet:\n' + '\n'.join(text.splitlines()[-12:])
        except Exception:
            pass
        if self.radicale_proc is not None and self.radicale_proc.poll() is None:
            try:
                self.radicale_proc.terminate()
            except Exception:
                pass
        raise RuntimeError('Radicale konnte nicht gestartet werden.' + details + f'\n\nVollständiges Log: {radicale_log}')

    def stop_radicale(self):
        if self.radicale_proc is not None and self.radicale_proc.poll() is None:
            self.radicale_proc.terminate()
            try:
                self.radicale_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.radicale_proc.kill()
            self.radicale_proc = None
            try:
                if getattr(self, '_radicale_log_handle', None):
                    self._radicale_log_handle.close()
                    self._radicale_log_handle = None
            except Exception:
                pass
            self.log('Radicale gestoppt.')
            return
        if self.radicale_running():
            self.log('Radicale läuft, wurde aber nicht von dieser App gestartet; Prozess wird nicht beendet.')

    @staticmethod
    def _open_serial(port):
        ser = serial.Serial(port=port, baudrate=115200, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_TWO, timeout=0.1, write_timeout=0.75, xonxoff=False, rtscts=False, dsrdtr=False)
        try:
            ser.rts = False
            ser.dtr = True
        except Exception:
            pass
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        return ser

    def _make_file_logger(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')

        def work_log(msg=''):
            self.log(msg)
            with path.open('a', encoding='utf-8') as f:
                f.write(str(msg) + '\n')
        return work_log

    def start_backup(self):
        port = self._selected_port()
        if not port:
            return
        self.log('Starte dokumentiertes IC35-Komplettbackup …')
        self.log('Für das Manager-Protokoll bitte den Sync-Knopf an der SyncStation drücken.')
        self._start_worker(self._backup_worker, port)

    def _backup_worker(self, port):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = LOGS / f'IC35_Backup_v1.6.0_{stamp}.log'
        backup_file = BACKUPS / f'database_{stamp}.org'
        meta_file = BACKUPS / f'database_{stamp}.json'
        work_log = self._make_file_logger(log_file)
        ser = None
        try:
            ser = self._open_serial(port)
            work_log(f'{port} geöffnet (Manager-Protokoll 115200/8N2).')
            mgr.manager_connect(ser, work_log)

            def progress(done, total):
                self.msg_queue.put(('progress', done))
                work_log(f'Backup-Fortschritt: {done}/{total} Blöcke')
            result = mgr.backup_database(ser, backup_file, work_log, progress)
            mgr.manager_disconnect(ser, work_log)
            meta = {'created_at': datetime.now().isoformat(), 'port': port, 'format': 'IC35 database.org compatible backup', **result}
            meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
            self.msg_queue.put(('backup_done', result))
        except Exception as exc:
            work_log(f'FEHLER: {exc}')
            self.msg_queue.put(('error', str(exc)))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

    def start_full_sync(self):
        port = self._selected_port()
        if not port:
            return
        if not GOOGLE_STATE.exists() or not GOOGLE_TOKEN.exists():
            try:
                config = read_tasks_config()
                if config.get('enabled') and config.get('list_id'):
                    self._start_worker(self._direct_tasks_worker, port, config)
                    return
            except Exception as exc:
                messagebox.showerror('Aufgaben-Einstellungen', str(exc))
                return
            messagebox.showerror('Google Kalender', "Google Kalender ist noch nicht vollständig verbunden. Bitte einmal unter 'Einstellungen & Werkzeuge' auf 'Google verbinden' klicken.")
            return
        self._set_stage('Vorbereitung …')
        self.log('Starte FINALEN Gesamt-Sync v3.3.0a1 …')
        self._start_worker(self._full_sync_worker, port)

    @staticmethod
    def _schedule_records_from_export(export: dict) -> list[dict]:
        return [r for r in export.get('databases', {}).get('Schedule', {}).get('records', []) if not r.get('error') and (not r.get('deleted'))]

    @staticmethod
    def _replace_or_append_schedule_record(export: dict, verify: dict):
        db = export.setdefault('databases', {}).setdefault('Schedule', {'records': []})
        records = db.setdefault('records', [])
        rid = int(verify.get('record_id', 0))
        for i, rec in enumerate(records):
            if int(rec.get('record_id', -1)) == rid:
                verify = dict(verify)
                verify['index'] = rec.get('index', i)
                records[i] = verify
                db['count'] = len(records)
                return
        verify = dict(verify)
        verify['index'] = len(records)
        records.append(verify)
        db['count'] = len(records)

    @staticmethod
    def _remove_export_schedule_record(export: dict, record_id: int):
        db = export.setdefault('databases', {}).setdefault('Schedule', {'records': []})
        rid = int(record_id)
        db['records'] = [r for r in db.setdefault('records', []) if int(r.get('record_id', -1)) != rid]
        db['count'] = len(db['records'])

    def _full_sync_worker(self, port):
        run_lock = None
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = LOGS / f'IC35_FullSync_v3.3.0a1_{stamp}.log'
        report_path = REPORTS / f'FullSync_v3.3.0a1_{stamp}.json'
        work_log = self._make_file_logger(log_file)
        ser = None
        sync_started = datetime.now(timezone.utc).isoformat()
        contact_stats = {'created': 0, 'updated': 0, 'deleted_ic35': 0, 'deleted_carddav': 0, 'published': 0}
        calendar_stats = {'g_create': 0, 'g_update': 0, 'g_delete': 0, 'i_create': 0, 'i_update': 0, 'i_delete': 0, 'conflicts': 0, 'skipped': 0}
        try:
            run_lock = memo_sync.acquire_run_lock(APPDATA)
            self.msg_queue.put(('sound', 'start'))
            tasks_config = read_tasks_config()
            tasks_service = None
            tasks_remote = None
            if tasks_config.get('enabled'):
                tasks_service = gtasks.service(GOOGLE_CREDENTIALS, APPDATA / 'google_tasks_token.json')
                tasks_remote = gtasks.snapshot(tasks_service, tasks_config['list_id'])
            self.msg_queue.put(('stage', '1/4 · Thunderbird & Google lesen …'))
            work_log('=== 1/4 Thunderbird/CardDAV + Google vorbereiten ===')
            self.start_radicale()
            if self.radicale_proc is None or self.radicale_proc.poll() is not None:
                raise RuntimeError('Radicale konnte nicht gestartet werden.')
            self.stop_radicale()
            contact_snapshot = fetch_storage_snapshot(STORAGE, logger=work_log)
            self.start_radicale()
            svc = gcal.service(GOOGLE_CREDENTIALS, GOOGLE_TOKEN, interactive=False)
            if svc is None:
                raise RuntimeError("Google-Anmeldung ist nicht mehr gültig. Bitte 'Google verbinden' ausführen.")
            gstate = gcal.load_state(GOOGLE_STATE)
            if not gstate.get('calendar_id'):
                raise RuntimeError("Bitte zuerst einen Google-Kalender auswählen.")
            self.msg_queue.put(('stage', '2/4 · SyncStation drücken: synchronisieren'))
            work_log('Bitte jetzt den Sync-Knopf an der SyncStation einmal drücken.')
            work_log("Vollbackup nur auf Anfrage über 'Nur Backup'.")
            proto.PORT = port
            proto.LOGFILE = log_file
            proto.EXPORTFILE = EXPORTS / f'IC35_raw_fullsync_{stamp}.json'
            proto.log = work_log
            ser = self._open_serial(port)
            if not proto.do_welcome_and_reopen(ser):
                raise RuntimeError('WELCOME-Handshake für Gesamt-Sync fehlgeschlagen.')
            identity = proto.identify(ser)
            if not identity:
                raise RuntimeError('IC35 konnte nicht identifiziert werden.')
            if not proto.power_request(ser):
                raise RuntimeError('Power-Request fehlgeschlagen.')
            auth = proto.authenticate_once(ser, '')
            if auth is None or auth[:2] != b'\x01\x01':
                raise RuntimeError('Authentication nicht erfolgreich.')
            proto.read_sync_info(ser)
            export = proto.read_and_export_databases(ser, identity)
            memo_plan = memo_sync.prepare(self._notes_config(), APPDATA, export)
            tasks_plan = gtasks.prepare(tasks_config, APPDATA, export, tasks_service, tasks_remote)
            contact_plan = analyze_contact_two_way(export, contact_snapshot, STATE, logger=work_log)
            if contact_plan['conflicts']:
                raise RuntimeError(f"Kontakt-Konflikt: {len(contact_plan['conflicts'])} Kontakt(e) wurden auf beiden Seiten unterschiedlich geändert.")
            if contact_plan['delete_locked']:
                raise RuntimeError(f"Kontakt-Löschschutz: {len(contact_plan['delete_locked'])} Löschung(en) sind nicht eindeutig abgesichert.")
            schedule_records = self._schedule_records_from_export(export)
            calendar_plan = gcal.build_calendar_two_way_plan(svc, gstate, schedule_records)
            calendar_stats['conflicts'] = len(calendar_plan['conflicts'])
            calendar_stats['skipped'] = len(calendar_plan['skipped'])
            report = {'created_at': datetime.now().isoformat(), 'device': identity, 'contact_plan': contact_plan, 'calendar_summary': gcal.summarize_calendar_plan(calendar_plan), 'calendar_conflicts': calendar_plan['conflicts'], 'calendar_skipped': calendar_plan['skipped'], 'tasks_operations': tasks_plan['operations'] if tasks_plan else [], 'tasks_skipped': tasks_plan['skipped'] if tasks_plan else []}
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if calendar_plan['conflicts']:
                details = '; '.join((str(x.get('reason') or x.get('type')) for x in calendar_plan['conflicts'][:4]))
                raise RuntimeError(f"Kalender-Konflikt: {len(calendar_plan['conflicts'])} Termin(e). {details} Bericht: {report_path}")
            work_log('=== Gesamtplan ===')
            work_log(f"Kontakte: neu→IC35={len(contact_plan['new_to_ic35'])}, update→IC35={len(contact_plan['update_to_ic35'])}, delete→IC35={len(contact_plan['delete_to_ic35'])}, IC35→TB={len(contact_plan['publish_from_ic35'])}, delete→TB={len(contact_plan['delete_from_carddav'])}")
            work_log(f'Kalender: {gcal.summarize_calendar_plan(calendar_plan)}')
            if calendar_plan.get('google_window'):
                gw = calendar_plan['google_window']
                work_log(f"Google-Bestandsabgleich: {gw.get('count', 0)} Event(s) im Fenster {gw.get('time_min')} .. {gw.get('time_max')}")
            for item in calendar_plan['skipped']:
                work_log(f"KALENDER ÜBERSPRUNGEN: {item.get('type')} {item.get('summary', '')} {item.get('reason', '')}")
            for group in (contact_plan['unchanged'], contact_plan['publish_from_ic35'], contact_plan['update_to_ic35']):
                for item in group:
                    rid = item.get('record_id')
                    resource = item.get('resource', '')
                    if rid and resource:
                        remember_address_binding(STATE, int(rid), resource, item.get('uid', ''), logger=work_log)
            afd = None
            if contact_plan['delete_to_ic35'] or contact_plan['update_to_ic35'] or contact_plan['new_to_ic35']:
                afd = proto.open_database(ser, 'Addresses')
                if afd is None:
                    raise RuntimeError('Addresses konnte nicht geöffnet werden.')
            try:
                for planned in contact_plan['delete_to_ic35']:
                    rid = int(planned.get('record_id', 0))
                    baseline_sem = planned.get('baseline_semantic', {})
                    count_before = proto.read_database_count(ser, 'Addresses', afd)
                    raw = proto.read_record_raw_by_id(ser, 'Addresses', afd, rid)
                    if raw is None:
                        raise RuntimeError(f'Kontakt Record-ID {rid} vor DELETE nicht lesbar.')
                    before = proto.decode_record('Addresses', raw, 0)
                    if contact_record_to_semantic(before) != baseline_sem:
                        raise RuntimeError(f'Kontakt Record-ID {rid} änderte sich nach Planung; DELETE abgebrochen.')
                    proto.delete_address_record(ser, afd, rid)
                    proto.verify_address_record_absent(ser, afd, rid, expected_count=count_before - 1)
                    self._remove_export_address_record(export, rid)
                    contact_stats['deleted_ic35'] += 1
                for planned in contact_plan['update_to_ic35']:
                    rid = int(planned.get('record_id', 0))
                    device_rec = self._find_export_address_record(export, rid)
                    if not device_rec:
                        raise RuntimeError(f'Kontakt Record-ID {rid} fehlt.')
                    existing = self._address_record_values(device_rec)
                    fields = contact_semantic_to_ic35_fields(planned.get('thunderbird', {}))
                    for key in ('category-id', '(def.)1', '(def.)2', 'category'):
                        if key in existing:
                            fields[key] = existing[key]
                    raw = proto.read_record_raw_by_id(ser, 'Addresses', afd, rid)
                    if raw is None:
                        raise RuntimeError(f'Kontakt Record-ID {rid} vor UPDATE nicht lesbar.')
                    rid2, file_id = proto.update_address_record(ser, afd, rid, fields)
                    if rid2 != rid:
                        raise RuntimeError('Kontakt-UPDATE wechselte unerwartet die Record-ID.')
                    raw_after = proto.read_record_raw_by_id(ser, 'Addresses', afd, rid)
                    verify = proto.decode_record('Addresses', raw_after, 0)
                    self._verify_address_fields(verify, fields)
                    self._replace_or_append_address_record(export, verify)
                    contact_stats['updated'] += 1
                for planned in contact_plan['new_to_ic35']:
                    resource = planned.get('resource', '')
                    fields = contact_semantic_to_ic35_fields(planned.get('semantic', {}))
                    new_rid, file_id = proto.write_new_address_record(ser, afd, fields)
                    raw = proto.read_record_raw_by_id(ser, 'Addresses', afd, new_rid)
                    verify = proto.decode_record('Addresses', raw, 0)
                    self._verify_address_fields(verify, fields)
                    self._replace_or_append_address_record(export, verify)
                    uid = planned.get('uid', '')
                    remember_address_binding(STATE, new_rid, resource, uid, logger=work_log)
                    snap_item = contact_snapshot.get('addressbook', {}).get(resource, {})
                    original_content = snap_item.get('content', '')
                    if not original_content:
                        raise RuntimeError(f'Neue CardDAV-Ressource {resource} fehlt im eingefrorenen Snapshot.')
                    mark_address_resource_ic35_id_dav(f'http://{HOST}:{RADICALE_PORT}', resource, original_content, new_rid, file_id, logger=work_log)
                    contact_stats['created'] += 1
            finally:
                if afd is not None:
                    proto.close_database(ser, 'Addresses', afd)
            schedule_create_bindings = []
            schedule_update_bindings = []
            sfd = None
            device_calendar_writes = calendar_plan['google_to_ic35_create'] or calendar_plan['google_to_ic35_update'] or calendar_plan['google_to_ic35_delete']
            if device_calendar_writes:
                sfd = proto.open_database(ser, 'Schedule')
                if sfd is None:
                    raise RuntimeError('Schedule konnte nicht geöffnet werden.')
            try:
                for item in calendar_plan['google_to_ic35_delete']:
                    rid = int(item['record_id'])
                    raw = proto.read_record_raw_by_id(ser, 'Schedule', sfd, rid)
                    if raw is None:
                        raise RuntimeError(f'Schedule Record-ID {rid} vor DELETE nicht lesbar.')
                    live = proto.decode_record('Schedule', raw, -1)
                    live_sem = gcal.ic35_schedule_semantic(live)
                    if live_sem != item.get('baseline_semantic'):
                        raise RuntimeError(f'Schedule Record-ID {rid} änderte sich nach Planung; DELETE abgebrochen.')
                    count_before = proto.read_database_count(ser, 'Schedule', sfd)
                    proto.delete_schedule_record(ser, sfd, rid)
                    proto.verify_schedule_record_absent(ser, sfd, rid, expected_count=count_before - 1)
                    self._remove_export_schedule_record(export, rid)
                    calendar_stats['g_delete'] += 1
                for item in calendar_plan['google_to_ic35_update']:
                    rid = int(item['record_id'])
                    fields = item['fields']
                    raw = proto.read_record_raw_by_id(ser, 'Schedule', sfd, rid)
                    if raw is None:
                        raise RuntimeError(f'Schedule Record-ID {rid} vor UPDATE nicht lesbar.')
                    live = proto.decode_record('Schedule', raw, -1)
                    if gcal.ic35_schedule_semantic(live) != item.get('baseline_semantic'):
                        raise RuntimeError(f'Schedule Record-ID {rid} änderte sich nach Planung; UPDATE abgebrochen.')
                    rid2, file_id = proto.update_schedule_record(ser, sfd, rid, fields)
                    if rid2 != rid:
                        raise RuntimeError('Schedule-UPDATE wechselte Record-ID.')
                    raw_after = proto.read_record_raw_by_id(ser, 'Schedule', sfd, rid)
                    verify = proto.decode_record('Schedule', raw_after, -1)
                    self._verify_schedule_create(verify, fields)
                    self._replace_or_append_schedule_record(export, verify)
                    schedule_update_bindings.append((item['event'], rid, file_id))
                    calendar_stats['g_update'] += 1
                for item in calendar_plan['google_to_ic35_create']:
                    event = item['event']
                    fields = item['fields']
                    new_rid, file_id = proto.write_new_schedule_record(ser, sfd, fields)
                    raw = proto.read_record_raw_by_id(ser, 'Schedule', sfd, new_rid)
                    verify = proto.decode_record('Schedule', raw, -1)
                    self._verify_schedule_create(verify, fields)
                    self._replace_or_append_schedule_record(export, verify)
                    schedule_create_bindings.append((event, new_rid, file_id))
                    calendar_stats['g_create'] += 1
            finally:
                if sfd is not None:
                    proto.close_database(ser, 'Schedule', sfd)
            self.msg_queue.put(('stage', '2/4 · Notizen synchronisieren …'))
            memo_sync.apply(memo_plan, ser, export, work_log)
            self.msg_queue.put(('stage', '2/4 · Google Tasks / Zu Erledigen synchronisieren …'))
            tasks_stats = gtasks.apply(tasks_plan, ser, export, work_log)
            final_schedule_records = self._schedule_records_from_export(export)
            proto.disconnect(ser)
            ser.close()
            ser = None
            self.msg_queue.put(('stage', '3/4 · Thunderbird & Google aktualisieren …'))
            work_log('')
            work_log('=== 3/4 Änderungen nach Thunderbird/CardDAV und Google veröffentlichen ===')
            if not self.radicale_running():
                self.start_radicale()
            for planned in contact_plan['delete_from_carddav']:
                resource = planned.get('resource', '')
                if not resource:
                    raise RuntimeError('CardDAV-DELETE ohne Ressourcenname.')
                status = delete_address_resource_dav(f'http://{HOST}:{RADICALE_PORT}', resource, logger=work_log)
                if status in (200, 204, 404):
                    contact_stats['deleted_carddav'] += 1
            pub = publish_contacts_dav_nolist(export, f'http://{HOST}:{RADICALE_PORT}', STATE, logger=work_log)
            contact_stats['published'] = pub['contacts']
            final_by_rid = {int(r.get('record_id', 0)): r for r in final_schedule_records if int(r.get('record_id', 0) or 0) > 0}
            for event, rid, file_id in schedule_create_bindings + schedule_update_bindings:
                rec = final_by_rid.get(int(rid))
                fields = gcal.ic35_schedule_snapshot(rec).get('fields', {}) if rec else {}
                gcal.remember_binding(GOOGLE_STATE, event, rid, file_id, fields)
            for item in calendar_plan['google_to_ic35_delete']:
                gcal.forget_binding(GOOGLE_STATE, item['event_id'])
            for item in calendar_plan['bind_pairs']:
                rec = item['record']
                rid = int(item['record_id'])
                fields = gcal.ic35_schedule_snapshot(rec).get('fields', {})
                gcal.remember_binding(GOOGLE_STATE, item['event'], rid, int(rec.get('file_id', 8)), fields)
            for item in calendar_plan['converged']:
                rec = item['record']
                rid = int(item['record_id'])
                fields = gcal.ic35_schedule_snapshot(rec).get('fields', {})
                gcal.remember_binding(GOOGLE_STATE, item['event'], rid, int(rec.get('file_id', 8)), fields)
            for item in calendar_plan['ic35_to_google_delete']:
                rid = int(item['record_id'])
                event_id = item['event_id']
                current_event, delete_request, verified_etag = gcal.prepare_google_event_delete_for_ic35(svc, gcal.load_state(GOOGLE_STATE), event_id, item['binding'], rid)
                google_backup = EXPORTS / f'Google_event_before_delete_{stamp}_{rid}.json'
                google_backup.write_text(json.dumps({'saved_at': datetime.now().isoformat(), 'calendar_id': gstate.get('calendar_id'), 'calendar_name': gstate.get('calendar_name'), 'ic35_record_id': rid, 'verified_etag': verified_etag, 'google_event': current_event}, ensure_ascii=False, indent=2), encoding='utf-8')
                gcal.execute_prepared_google_delete(delete_request)
                gcal.forget_binding(GOOGLE_STATE, event_id)
                calendar_stats['i_delete'] += 1
            for item in calendar_plan['ic35_to_google_update']:
                updated_event = gcal.patch_google_event_from_ic35(svc, gcal.load_state(GOOGLE_STATE), item['event_id'], item['binding'], item['record'])
                rec = item['record']
                fields = gcal.ic35_schedule_snapshot(rec).get('fields', {})
                gcal.remember_binding(GOOGLE_STATE, updated_event, int(item['record_id']), int(rec.get('file_id', 8)), fields)
                calendar_stats['i_update'] += 1
            for item in calendar_plan['ic35_to_google_create']:
                event, _created = gcal.create_google_event_from_ic35(svc, gcal.load_state(GOOGLE_STATE), item['record'])
                rec = item['record']
                rid = int(item['record_id'])
                fields = gcal.ic35_schedule_snapshot(rec).get('fields', {})
                gcal.remember_binding(GOOGLE_STATE, event, rid, int(rec.get('file_id', 8)), fields)
                calendar_stats['i_create'] += 1
            for item in calendar_plan['cleanup_bindings']:
                gcal.forget_binding(GOOGLE_STATE, item['event_id'])
            gcal.save_ic35_schedule_baseline(GOOGLE_STATE, final_schedule_records)
            state2 = gcal.load_state(GOOGLE_STATE)
            state2['baseline_utc'] = sync_started
            gcal.save_state(GOOGLE_STATE, state2)
            self.msg_queue.put(('stage', '4/4 · Fertig'))
            counts = {'contacts': len([r for r in export.get('databases', {}).get('Addresses', {}).get('records', []) if not r.get('error') and (not r.get('deleted'))]), 'calendar': len(final_schedule_records), 'memos': len([r for r in export.get('databases', {}).get('Memo', {}).get('records', []) if not r.get('error') and (not r.get('deleted'))])}
            work_log('=== GESAMT-SYNC ERFOLGREICH ===')
            work_log(f'Kontakte: {contact_stats}')
            work_log(f'Kalender: {calendar_stats}')
            work_log(f'Bericht: {report_path}')
            self.msg_queue.put(('full_sync_done', {'device': identity, 'counts': counts, 'contacts': contact_stats, 'calendar': calendar_stats, 'tasks': tasks_stats, 'report_path': str(report_path), 'log_path': str(log_file)}))
        except Exception as exc:
            work_log(f'FEHLER / SICHERHEITSABBRUCH: {exc}')
            self.msg_queue.put(('error', str(exc)))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            try:
                if run_lock is not None and (not self.radicale_running()):
                    self.start_radicale()
            except Exception as exc:
                self.log(f'WARNUNG: Radicale nach Gesamt-Sync nicht neu gestartet: {exc}')
            if run_lock is not None:
                run_lock.close()

    def _verify_address_fields(self, verify: dict, fields: dict):
        got = self._address_record_values(verify)
        mismatches = []
        for name, _ftype in proto.FIELD_SPECS['Addresses']:
            expected_value = fields.get(name, 0 if name == 'category-id' else '')
            actual_value = got.get(name, 0 if name == 'category-id' else '')
            if str(actual_value) != str(expected_value):
                mismatches.append((name, expected_value, actual_value))
        if mismatches:
            details = '; '.join((f'{k}: erwartet={a!r}, gelesen={b!r}' for k, a, b in mismatches[:8]))
            raise RuntimeError('Read-back weicht ab: ' + details)

    @staticmethod
    def _replace_or_append_address_record(export: dict, verify: dict):
        db = export.setdefault('databases', {}).setdefault('Addresses', {'records': []})
        records = db.setdefault('records', [])
        rid = int(verify.get('record_id', 0))
        for i, rec in enumerate(records):
            if int(rec.get('record_id', -1)) == rid:
                verify = dict(verify)
                verify['index'] = rec.get('index', i)
                records[i] = verify
                db['count'] = len(records)
                return
        verify = dict(verify)
        verify['index'] = len(records)
        records.append(verify)
        db['count'] = len(records)

    @staticmethod
    def _remove_export_address_record(export: dict, record_id: int):
        db = export.setdefault('databases', {}).setdefault('Addresses', {'records': []})
        records = db.setdefault('records', [])
        rid = int(record_id)
        db['records'] = [r for r in records if int(r.get('record_id', -1)) != rid]
        db['count'] = len(db['records'])

    def connect_google_calendar(self):
        global GOOGLE_STATE
        try:
            if not GOOGLE_CREDENTIALS.exists():
                src = filedialog.askopenfilename(title='Google Desktop-OAuth-Datei auswählen', filetypes=[('JSON', '*.json')])
                if not src:
                    return
                gcal.install_credentials(Path(src), GOOGLE_CREDENTIALS)
            svc = gcal.service(GOOGLE_CREDENTIALS, GOOGLE_TOKEN, interactive=True)
            calendars = calendar_setup.writable_calendars(svc)
            if not calendars:
                raise ValueError('Keine beschreibbaren Kalender in diesem Konto gefunden.')
            dialog = tk.Toplevel(self)
            dialog.title('Google-Kalender auswählen')
            dialog.transient(self)
            dialog.grab_set()
            frame = ttk.Frame(dialog, padding=18)
            frame.pack(fill='both', expand=True)
            ttk.Label(frame, text='Kalender für den bidirektionalen Abgleich:').pack(anchor='w')
            combo = ttk.Combobox(frame, state='readonly', width=60,
                values=[f"{i+1}. {c.get('summary', c['id'])}" for i,c in enumerate(calendars)])
            combo.pack(pady=10)
            combo.current(0)
            ttk.Label(frame, wraplength=500, text='Beim ersten Lauf werden vorhandene Termine zusammengeführt. '
                'Ein Zielwechsel verschiebt keine Termine; jedes Kalenderziel behält seinen eigenen Abgleichstand.').pack()
            def save():
                global GOOGLE_STATE
                try:
                    GOOGLE_STATE = calendar_setup.select(APPDATA, calendars[combo.current()])
                    self.log('Google-Kalender ausgewählt. Bereit für den ersten Abgleich.')
                    dialog.destroy()
                except Exception as exc:
                    messagebox.showerror('Kalenderauswahl', str(exc), parent=dialog)
            ttk.Button(frame, text='Speichern', command=save).pack(pady=10)
        except Exception as exc:
            messagebox.showerror('Google Kalender', str(exc))

    @staticmethod
    def _schedule_record_values(record: dict) -> dict:
        return {name: record.get('fields', {}).get(name, {}).get('value', '') for name, _ftype in proto.FIELD_SPECS['Schedule']}

    def _verify_schedule_create(self, verify: dict, fields: dict):
        got = self._schedule_record_values(verify)
        checks = {'Betreff': (str(got.get('Betreff', '')), str(fields.get('Betreff', ''))), 'Start(Datum)': (str(got.get('Start(Datum)', ''))[:8], str(fields.get('Start(Datum)', ''))[:8]), 'Ende(Datum)': (str(got.get('Ende(Datum)', ''))[:8], str(fields.get('Ende(Datum)', ''))[:8]), 'Start(Zeit)': (str(got.get('Start(Zeit)', ''))[:4], str(fields.get('Start(Zeit)', ''))[:4]), 'Ende(Zeit)': (str(got.get('Ende(Zeit)', ''))[:4], str(fields.get('Ende(Zeit)', ''))[:4]), 'Notizen': (str(got.get('Notizen', '')), str(fields.get('Notizen', '')))}
        mismatches = [f'{k}: erwartet={exp!r}, gelesen={actual!r}' for k, (actual, exp) in checks.items() if actual != exp]
        try:
            alarm_before = int(got.get('AlrmBef', 0) or 0)
            alarm_repeat = int(got.get('AlrmRep', 0) or 0)
        except (TypeError, ValueError):
            alarm_before, alarm_repeat = (-1, -1)
        if alarm_before != 0:
            mismatches.append(f'AlrmBef: erwartet 0, gelesen={alarm_before!r}')
        if alarm_repeat & 15 != 0:
            mismatches.append(f'AlrmRep Wiederholungsbits: erwartet 0, gelesen=0x{alarm_repeat & 255:02X}')
        if mismatches:
            raise RuntimeError('Schedule Read-back weicht ab: ' + '; '.join(mismatches[:8]))

    @staticmethod
    def _find_export_address_record(export: dict, record_id: int):
        for rec in export.get('databases', {}).get('Addresses', {}).get('records', []):
            if rec.get('record_id') == int(record_id):
                return rec
        return None

    @staticmethod
    def _address_record_values(rec: dict) -> dict:
        out = {}
        for name, item in rec.get('fields', {}).items():
            if isinstance(item, dict):
                out[name] = item.get('value', '')
            else:
                out[name] = item
        return out

    def _update_last_backup_label(self):
        backups = sorted(BACKUPS.glob('database_*.org'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            self.backup_var.set('Letztes Komplettbackup: noch keines')
            return
        p = backups[0]
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime('%d.%m.%Y %H:%M:%S')
        self.backup_var.set(f'Letztes Komplettbackup: {when} · {p.name} · {p.stat().st_size} Byte')

    def _direct_tasks_worker(self, port, config):
        work_log = self._make_file_logger(LOGS / f'IC35_DirectTasks_{datetime.now():%Y%m%d_%H%M%S}.log')
        try:
            result = direct_tasks_sync.run(port, config, APPDATA, GOOGLE_CREDENTIALS, self._open_serial, lambda kind, value: self.msg_queue.put((kind, value)), work_log)
            self.msg_queue.put(('direct_tasks_done', result))
        except Exception as exc:
            work_log(f'Aufgaben-Sync gestoppt: {exc}')
            self.msg_queue.put(('error', str(exc)))

    def configure_tasks(self):
        try:
            config = read_tasks_config()
        except Exception as exc:
            messagebox.showerror('Aufgaben-Einstellungen', str(exc))
            return
        dialog = tk.Toplevel(self)
        dialog.title('Zu Erledigen ↔ Google Tasks')
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        enabled = tk.BooleanVar(value=config.get('enabled', False))
        ttk.Checkbutton(frame, text='Aufgaben in beide Richtungen synchronisieren', variable=enabled).pack(anchor='w')
        status = tk.StringVar(value='Aktuelle Liste: ' + config.get('list_title', 'noch keine'))
        ttk.Label(frame, textvariable=status, wraplength=520).pack(anchor='w', pady=8)
        selection = tk.StringVar()
        combo = ttk.Combobox(frame, textvariable=selection, state='readonly', width=55)
        combo.pack(fill='x')
        lists = []
        results = queue.Queue()
        busy = [False]

        def load():
            if not GOOGLE_CREDENTIALS.exists():
                source = filedialog.askopenfilename(parent=dialog, title='Google Desktop-OAuth-Datei auswählen (credentials.json)', filetypes=[('JSON-Datei', '*.json')])
                if not source:
                    return
                try:
                    gcal.install_credentials(Path(source), GOOGLE_CREDENTIALS)
                except Exception as exc:
                    messagebox.showerror('Google Tasks', str(exc), parent=dialog)
                    return
            busy[0] = True
            load_btn.config(state='disabled')
            save_btn.config(state='disabled')
            status.set('Google-Anmeldung / Aufgabenlisten werden geladen …')

            def worker():
                try:
                    svc = gtasks.service(GOOGLE_CREDENTIALS, APPDATA / 'google_tasks_token.json', interactive=True)
                    results.put((True, gtasks.tasklists(svc)))
                except Exception as exc:
                    results.put((False, str(exc)))
            threading.Thread(target=worker, daemon=True).start()
            dialog.after(150, poll)

        def poll():
            if not dialog.winfo_exists():
                return
            try:
                ok, value = results.get_nowait()
            except queue.Empty:
                dialog.after(150, poll)
                return
            busy[0] = False
            load_btn.config(state='normal')
            save_btn.config(state='normal')
            if not ok:
                status.set('Verbindung fehlgeschlagen')
                messagebox.showerror('Google Tasks', value + '\n\nBei API-/Zugriffsfehlern: Google Tasks API im bestehenden Cloud-Projekt aktivieren und Tasks-Berechtigung freigeben. Siehe README_AUFGABEN.md.', parent=dialog)
                return
            lists[:] = value
            combo['values'] = [f"{i + 1}. {item.get('title', 'Ohne Namen')}" for i, item in enumerate(lists)]
            current = next((i for i, item in enumerate(lists) if item['id'] == config.get('list_id')), None)
            if current is not None:
                combo.current(current)
            elif lists:
                combo.current(0)
            status.set(f'{len(lists)} Aufgabenliste(n) geladen. Bitte Ziel auswählen.')
        load_btn = ttk.Button(frame, text='Google Tasks verbinden / Listen laden', command=load)
        load_btn.pack(anchor='w', pady=8)
        ttk.Label(frame, wraplength=520, text='Der erste Lauf führt vorhandene Aufgaben zusammen. Danach werden auch Löschungen und der Erledigt-Status in beide Richtungen übernommen.\n\nÜbertragen werden Titel, Text und Aufgabendatum (ohne Uhrzeit). IC35-Priorität, Kategorie und Startdatum bleiben lokal erhalten. Unteraufgaben, deren Eltern und zugewiesene Aufgaben werden übersprungen.\n\nGoogle Tasks benötigt eine zusätzliche Google-Freigabe. Die Kalenderanmeldung bleibt bestehen. Eine eigene Aufgabenliste für den IC35 ist möglich.').pack(anchor='w', pady=8)

        def save():
            try:
                new = dict(config)
                new['enabled'] = enabled.get()
                new['include_in_all'] = enabled.get()
                index = combo.current()
                if index >= 0 and lists:
                    new.update(list_id=lists[index]['id'], list_title=lists[index].get('title', ''))
                if new['enabled'] and (not new.get('list_id')):
                    raise ValueError('Bitte zuerst Google Tasks verbinden und eine Liste auswählen.')
                memo_sync.atomic_json(APPDATA / 'google_tasks_settings.json', new)
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror('Aufgaben-Ziel', str(exc), parent=dialog)
        save_btn = ttk.Button(frame, text='Speichern', command=save)
        save_btn.pack(anchor='e', pady=8)
        dialog.protocol('WM_DELETE_WINDOW', lambda: None if busy[0] else dialog.destroy())

    @staticmethod
    def _notes_config():
        return memo_sync.read_json(APPDATA / 'memo_settings.json', {'mode': 'Aus'})

    def configure_notes(self):
        try:
            cfg = self._notes_config()
        except Exception as exc:
            messagebox.showerror('Notiz-Einstellungen', f'memo_settings.json nicht lesbar: {exc}')
            return
        dialog = tk.Toplevel(self)
        dialog.title('Notiz-Synchronisation')
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        mode = tk.StringVar(value=cfg.get('mode', 'Aus'))
        folder = tk.StringVar(value=cfg.get('folder', ''))
        extension = tk.StringVar(value=cfg.get('extension', '.md'))
        ttk.Label(frame, text='Notiz-Ziel:').grid(row=0, column=0, sticky='w')
        ttk.Combobox(frame, textvariable=mode, values=memo_sync.MODES, state='readonly', width=35).grid(row=0, column=1, sticky='ew')
        ttk.Label(frame, text='Ordner:').grid(row=1, column=0, sticky='w', pady=8)
        ttk.Entry(frame, textvariable=folder, width=48).grid(row=1, column=1)

        def browse():
            selected = filedialog.askdirectory(parent=dialog, title='Vorhandenen Notizordner wählen')
            if selected:
                folder.set(selected)
        ttk.Button(frame, text='Auswählen …', command=browse).grid(row=1, column=2, padx=6)
        ttk.Label(frame, text='Neue Dateien:').grid(row=2, column=0, sticky='w')
        ttk.Combobox(frame, textvariable=extension, values=('.md', '.txt'), state='readonly', width=8).grid(row=2, column=1, sticky='w')
        ttk.Label(frame, wraplength=560, text='Cloud: Wähle einen bereits per Google Drive, OneDrive oder anderem Desktop-Client synchronisierten Ordner. Alle Dateien müssen offline verfügbar sein.\n\nJede .txt/.md-Datei direkt in diesem Ordner wird abgeglichen: erste Zeile = Betreff, Rest = Text. Maximal 60/255 Byte im IC35-Zeichensatz. Unterordner werden ignoriert.\n\nErster Lauf: Bestände zusammenführen. Danach auch Löschungen in beide Richtungen. Bei Konflikten wird angehalten. Verwende einen eigenen Ordner nur für IC35-Notizen. Ein Zielwechsel führt einen neuen Bestandsabgleich aus.').grid(row=3, column=0, columnspan=3, sticky='w', pady=14)

        def save():
            try:
                new = {'mode': mode.get(), 'extension': extension.get()}
                if mode.get() != 'Aus':
                    if not folder.get().strip():
                        raise ValueError('Bitte einen Notizordner auswählen.')
                    root, target_id = memo_sync.configure(folder.get())
                    new.update(folder=root, target_id=target_id)
                memo_sync.atomic_json(APPDATA / 'memo_settings.json', new)
                self.notes_status.set('Notizen: ' + new['mode'])
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror('Notiz-Ziel', str(exc), parent=dialog)
        ttk.Button(frame, text='Speichern', command=save).grid(row=4, column=2, sticky='e')

    def open_data_folder(self):
        APPDATA.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(APPDATA)
        else:
            webbrowser.open(APPDATA.as_uri())

    def on_close(self):
        try:
            self.stop_radicale()
        except Exception:
            pass
        self.destroy()
if __name__ == '__main__':
    App().mainloop()
