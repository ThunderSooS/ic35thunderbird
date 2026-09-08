"""Build a plain, uncompressed ZIP from an explicit source allowlist."""
import hashlib
import zipfile
from check_release import ROOT, release_files, check

check()
output = ROOT / 'dist' / 'IC35-Sync-3.3.0a1-source.zip'
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
    for path in release_files():
        name = 'ic35-sync/' + path.relative_to(ROOT).as_posix()
        info = zipfile.ZipInfo(name, (2026, 9, 8, 12, 0, 0))
        info.create_system = 0
        info.external_attr = 0x20
        archive.writestr(info, path.read_bytes())
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    for path in release_files():
        assert archive.read('ic35-sync/' + path.relative_to(ROOT).as_posix()) == path.read_bytes()
checksum = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.zip.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='ascii')
print(f'{output.name}: {output.stat().st_size} bytes; SHA256 {checksum}')
