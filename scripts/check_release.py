"""Offline release gate. Never reads the user's runtime data directory."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    'IC35_Thunderbird_Sync.py', 'bridge.py', 'calendar_setup.py', 'direct_tasks_sync.py',
    'google_calendar_bridge.py', 'google_tasks_bridge.py', 'ic35_protocol.py',
    'manager_protocol.py', 'memo_protocol.py', 'memo_sync.py', 'todo_protocol.py',
    'sync_sounds.py', 'radicale_windows_launcher.py', 'start.bat', 'install.bat',
    'requirements.txt', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md', '.gitignore', '.gitattributes',
}


def release_files():
    paths = [ROOT / name for name in sorted(ROOT_FILES)]
    for directory, suffix in (('docs', '.md'), ('tests', '.py'), ('scripts', '.py'), ('sounds', '.wav')):
        paths.extend(sorted((ROOT / directory).glob('*' + suffix)))
    paths.append(ROOT / '.github/workflows/check.yml')
    return paths


def check():
    forbidden = re.compile(r'(?:GOCSPX-[A-Za-z0-9_-]{8,}|ya29\.[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{25,}|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)')
    for path in release_files():
        if not path.is_file():
            raise ValueError(f'Missing release file: {path.relative_to(ROOT)}')
        if path.suffix == '.wav':
            continue
        text = path.read_text(encoding='utf-8')
        if forbidden.search(text):
            raise ValueError(f'Possible credential in {path.relative_to(ROOT)}')
        if path.suffix == '.py':
            compile(text, str(path), 'exec')
    tree = ast.parse((ROOT / 'IC35_Thunderbird_Sync.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'App')
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert not any('test' in name for name in methods), 'Legacy test method'
    full = methods['_full_sync_worker']
    assert not any(isinstance(n, ast.Attribute) and n.attr == 'backup_database' for n in ast.walk(full))
    assert any(isinstance(n, ast.Attribute) and n.attr == 'backup_database' for n in ast.walk(methods['_backup_worker']))
    assert not any(isinstance(n, ast.Attribute) and n.attr == 'askyesno' for n in ast.walk(methods['start_full_sync']))
    print(f'Release gate OK: {len(release_files())} allowlisted files; syntax and credential-pattern check passed.')


if __name__ == '__main__':
    check()
