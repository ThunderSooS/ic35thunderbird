import ast
import tempfile
from pathlib import Path
import sys
import types
import unittest
sys.modules.setdefault('serial', types.ModuleType('serial'))
import calendar_setup as setup


class PublicSetupTests(unittest.TestCase):
    def test_calendar_switch_and_rename_preserve_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            a = setup.select(td, {'id': 'a', 'summary': 'Mein Kalender'})
            state = setup.read_json(a)
            state['bindings'] = {'event': {'record_id': 1}}
            setup.atomic_json(a, state)
            b = setup.select(td, {'id': 'b', 'summary': 'Mein Kalender'})
            self.assertNotEqual(a, b)
            self.assertEqual(setup.read_json(b)['bindings'], {})
            setup.select(td, {'id': 'a', 'summary': 'Umbenannt'})
            self.assertEqual(setup.read_json(a)['bindings'], state['bindings'])
            self.assertEqual(setup.selected_state_path(td), a)

    def test_writable_calendar_pagination(self):
        pages = iter([{'items': [{'id':'a','accessRole':'reader'}, {'id':'b','accessRole':'owner'}], 'nextPageToken':'next'},
                      {'items': [{'id':'c','accessRole':'writer'}]}])
        svc = types.SimpleNamespace(calendarList=lambda: types.SimpleNamespace(
            list=lambda **kwargs: types.SimpleNamespace(execute=lambda: next(pages))))
        self.assertEqual([c['id'] for c in setup.writable_calendars(svc)], ['b','c'])

    def test_gui_has_no_removed_widget_references(self):
        source = Path('IC35_Thunderbird_Sync.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        assigned = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id=='self'}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id=='self':
                if n.attr.endswith(('_btn', '_combo', '_canvas', '_var')):
                    self.assertIn(n.attr, assigned)
        self.assertNotIn('v2.x', source)

    def test_calendar_planner_accepts_initial_empty_baseline(self):
        # Exercise the real planner with boundary adapters, without Google dependencies.
        source = ast.parse(Path('google_calendar_bridge.py').read_text(encoding='utf-8'))
        fn = next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='build_calendar_two_way_plan')
        namespace = {'list_google_events_for_reconciliation': lambda *a: ([], {}),
                     'list_current_bound_events': lambda *a: {},
                     'ic35_schedule_snapshot': lambda r: {},
                     'ic35_schedule_to_google_body': lambda *a: {},
                     'ic35_schedule_semantic': lambda r: {}}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'planner','exec'), namespace)
        plan = namespace['build_calendar_two_way_plan'](None, {'calendar_id':'new', 'bindings':{}}, [])
        self.assertEqual(plan['conflicts'], [])
        namespace.update({'list_google_events_for_reconciliation': lambda *a: ([{'id':'first'}], {}),
                          'event_to_ic35_fields': lambda e: {'Betreff':'Termin'},
                          '_sync_semantic_from_fields': lambda f: f})
        plan = namespace['build_calendar_two_way_plan'](None, {'calendar_id':'new','bindings':{}}, [])
        self.assertEqual(len(plan['google_to_ic35_create']), 1)


if __name__ == '__main__':
    unittest.main()
