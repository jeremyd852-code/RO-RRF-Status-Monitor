"""Static integrity and privacy checks for the portable package."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'build_tools'))
from release_files import SOURCE_FILES, DATA_FILES, VERSION, package_file_mapping, source_archive_files, assert_safe_archive_parts, release_metadata
DIST_ROOT = PROJECT_ROOT / 'dist'
PACKAGE_NAME = ''
PACKAGE_ROOT = DIST_ROOT
ZIP_PATH = DIST_ROOT
HASH_PATH = DIST_ROOT
RUNTIME_FILES = {'_internal/_tkinter.pyd', '_internal/tcl86t.dll', '_internal/tk86t.dll', '_internal/_tcl_data/init.tcl', '_internal/_tk_data/tk.tcl'}
REQUIRED_FILES = set(package_file_mapping()) | RUNTIME_FILES | {release_metadata()['executable']}
PAYLOAD_MAPPING = package_file_mapping()
FORBIDDEN_FILENAMES = {'config.json', 'status_data_cache.json', 'client_data_cache.json', 'unknown_data_journal.json', 'startup.log'}
FORBIDDEN_SUFFIXES = {'.rrf', '.pyc', '.log', '.bak', '.backup', '.tmp'}
FORBIDDEN_PATH_PARTS = {'.git', '__pycache__', 'build', 'tests', 'visual-output', '版本歷史', '開發任務'}
FORBIDDEN_TEXT: set[str] = set()
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.json', '.lua', '.lub', '.vbs', '.ps1', '.toml', '.ini', '.cfg', '.csv', '.xml', '.tcl'}
USER_PATH_PATTERN = re.compile('(?i)[a-z]:[\\\\/]+users[\\\\/]+[^\\\\/\\r\\n\\x00]+')
BINARY_PRIVATE_MARKERS = tuple((b'c:' + bytes((separator,)) + b'users' + bytes((separator,)) for separator in (47, 92)))

def relative_files(root: Path) -> set[str]:
    files = set()
    for path in (root, *root.rglob('*')):
        assert not path.is_symlink(), f'Symbolic link in package: {path.name}'
        assert not getattr(path, 'is_junction', lambda: False)(), f'Junction in package: {path.name}'
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files

def assert_privacy_safe(files: set[str]) -> None:
    for relative in files:
        path = Path(relative)
        assert path.suffix.lower() not in {'.py', '.ps1', '.bat', '.vbs', '.spec'}, relative
        assert path.name.casefold() not in FORBIDDEN_FILENAMES, relative
        assert not re.fullmatch('(?:config|status_data_cache|client_data_cache|unknown_data_journal)(?:\\.[^.]+)?\\.json', path.name, re.I), relative
        assert path.suffix.lower() not in FORBIDDEN_SUFFIXES, relative
        assert not {part.casefold() for part in path.parts} & FORBIDDEN_PATH_PARTS, relative
        assert relative in REQUIRED_FILES or relative.startswith('_internal/'), ('File outside package allowlist', relative)

def assert_content_privacy(relative: str, raw: bytes) -> None:
    """掃描整個發布包，不只檢查三個主要文字檔。"""
    lowered = raw.lower()
    for marker in BINARY_PRIVATE_MARKERS:
        assert marker.lower() not in lowered, (relative, marker)
    path = Path(relative)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return
    decoded = raw.decode('utf-16', errors='ignore') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig', errors='ignore')
    if not decoded.strip():
        decoded = raw.decode('cp950', errors='ignore')
    assert USER_PATH_PATTERN.search(decoded) is None, relative
    for forbidden in FORBIDDEN_TEXT:
        assert forbidden not in decoded, (relative, forbidden)

def scan_package_folder(files: set[str]) -> None:
    for relative in files:
        assert_content_privacy(relative, (PACKAGE_ROOT / relative).read_bytes())

def scan_package_zip() -> set[str]:
    files: set[str] = set()
    with zipfile.ZipFile(ZIP_PATH) as archive:
        seen: set[str] = set()
        for info in archive.infolist():
            name = info.orig_filename.replace('\\', '/')
            assert '\x00' not in name, 'NUL byte in ZIP path'
            assert not name.startswith('/'), 'Absolute ZIP path'
            parts = name.rstrip('/').split('/')
            assert_safe_archive_parts(parts)
            assert all((part not in {'', '.', '..'} and ':' not in part for part in parts)), ('Unsafe ZIP path', name)
            normalized = PurePosixPath(*parts).as_posix()
            assert normalized.casefold() not in seen, ('Duplicate ZIP path', name)
            seen.add(normalized.casefold())
            assert parts[0] == PACKAGE_NAME, ('ZIP entry outside package root', name)
            assert info.external_attr >> 16 & 61440 != 40960, ('ZIP symbolic link', name)
            if info.is_dir() or name.endswith('/'):
                continue
            assert len(parts) > 1, ('File replaces package root', name)
            relative = '/'.join(parts[1:])
            files.add(relative)
            raw = archive.read(info)
            assert_content_privacy(relative, raw)
            folder_file = PACKAGE_ROOT / relative
            assert folder_file.is_file(), ('ZIP file missing from package folder', relative)
            assert hashlib.sha256(raw).digest() == hashlib.sha256(folder_file.read_bytes()).digest(), ('ZIP and package folder differ', relative)
    assert files == relative_files(PACKAGE_ROOT), 'ZIP and package folder file lists differ'
    return files

def assert_source_consistency(files: set[str]) -> None:
    for relative, source_relative in sorted(PAYLOAD_MAPPING.items()):
        assert relative in files, relative
        source = PROJECT_ROOT / source_relative
        assert source.is_file(), ('Package source is missing', relative)
        assert source.read_bytes() == (PACKAGE_ROOT / relative).read_bytes(), ('Package differs from current source', relative)

def test_source_archive() -> None:
    archive_path = DIST_ROOT / f'{PACKAGE_NAME}-原始碼.zip'
    hash_path = DIST_ROOT / f'{PACKAGE_NAME}-原始碼.zip.sha256'
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == hash_path.read_text(encoding='ascii').strip()
    expected = source_archive_files(PROJECT_ROOT)
    root_name = PACKAGE_NAME + '-原始碼'
    seen: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = entry.orig_filename
            assert '\x00' not in name and '\\' not in name, 'Unsafe source ZIP path'
            parts = name.split('/')
            assert_safe_archive_parts(parts)
            assert all((part not in {'', '.', '..'} and ':' not in part for part in parts))
            assert len(parts) > 1 and parts[0] == root_name
            assert not entry.is_dir() and entry.external_attr >> 16 & 61440 != 40960
            relative = '/'.join(parts[1:])
            assert relative in expected and relative.casefold() not in seen, relative
            seen.add(relative.casefold())
            raw = archive.read(entry)
            assert raw == (PROJECT_ROOT / relative).read_bytes(), ('Source ZIP differs', relative)
            assert_content_privacy(relative, raw)
    assert seen == {name.casefold() for name in expected}, 'Incomplete source ZIP'

def assert_manifest_integrity() -> None:
    manifest = json.loads((PACKAGE_ROOT / 'data/catalog_manifest.json').read_text(encoding='utf-8-sig'))
    assert manifest.get('app_version') == VERSION, 'Manifest version mismatch'
    expected_data = {Path(name).name for name in DATA_FILES} - {'catalog_manifest.json'}
    entries = manifest.get('files', {})
    assert isinstance(entries, dict) and set(entries) == expected_data, 'Manifest data allowlist mismatch'
    for filename, digest in entries.items():
        assert hashlib.sha256((PACKAGE_ROOT / 'data' / filename).read_bytes()).hexdigest() == digest, ('Manifest content mismatch', filename)
    runtime = json.loads((PACKAGE_ROOT / 'data/runtime_status_index.json').read_text(encoding='utf-8-sig'))
    assert runtime.get('app_version') == VERSION, 'Runtime index version mismatch'

def test_package_folder() -> None:
    assert PACKAGE_ROOT.is_dir(), PACKAGE_ROOT
    files = relative_files(PACKAGE_ROOT)
    assert not REQUIRED_FILES - files, sorted(REQUIRED_FILES - files)
    assert_privacy_safe(files)
    scan_package_folder(files)
    assert_source_consistency(files)
    assert_manifest_integrity()
    catalog_names = ('可擴充資料庫.json', 'client_catalog.json', 'job_skill_catalog.json', 'job_status_index.json', 'status_catalog.json', 'status_classification.json')
    catalogs: dict[str, dict[str, object]] = {}
    for name in catalog_names:
        payload = json.loads((PACKAGE_ROOT / 'data' / name).read_text(encoding='utf-8-sig'))
        assert payload.get('game_id') == 'twro', name
        assert re.fullmatch('sha256:[0-9a-f]{64}', str(payload.get('source_signature', ''))), name
        assert re.fullmatch('\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\+08:00', str(payload.get('built_at', ''))), name
        catalogs[name] = payload
    classification = catalogs['status_classification.json']
    assert isinstance(classification, dict)
    assert classification.get('game_id') == 'twro'
    records = classification.get('records', {})
    assert isinstance(records, dict) and len(records) >= 90
    statistics = classification.get('statistics', {})
    assert isinstance(statistics, dict)
    subcategory_counts = statistics.get('subcategory_counts', {})
    assert isinstance(subcategory_counts, dict)
    assert statistics.get('record_count') == len(records)
    assert sum((int(value) for value in subcategory_counts.values())) == len(records)

def test_zip_and_hash() -> None:
    assert ZIP_PATH.is_file(), ZIP_PATH
    assert HASH_PATH.is_file(), HASH_PATH
    digest = hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()
    recorded = HASH_PATH.read_text(encoding='ascii').strip().split()[0].lower()
    assert recorded == digest
    files = scan_package_zip()
    assert not REQUIRED_FILES - files, sorted(REQUIRED_FILES - files)
    assert_privacy_safe(files)

def main() -> int:
    global DIST_ROOT, PACKAGE_ROOT, ZIP_PATH, HASH_PATH, PROJECT_ROOT, PACKAGE_NAME, REQUIRED_FILES, PAYLOAD_MAPPING
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', '--dist-root', type=Path, default=DIST_ROOT)
    parser.add_argument('--source-root', type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    DIST_ROOT = args.output_directory.resolve()
    PROJECT_ROOT = args.source_root.resolve()
    info = json.loads((PROJECT_ROOT / 'release-info.json').read_text(encoding='utf-8'))
    assert info['version'] == VERSION
    assert info == release_metadata(), 'Release metadata differs from the product source'
    PACKAGE_NAME = info['package_name']
    assert Path(PACKAGE_NAME).name == PACKAGE_NAME
    PAYLOAD_MAPPING = package_file_mapping()
    REQUIRED_FILES = set(PAYLOAD_MAPPING) | RUNTIME_FILES | {info['executable']}
    PACKAGE_ROOT = DIST_ROOT / PACKAGE_NAME
    ZIP_PATH = DIST_ROOT / f'{PACKAGE_NAME}.zip'
    HASH_PATH = DIST_ROOT / f'{PACKAGE_NAME}.zip.sha256'
    test_package_folder()
    test_zip_and_hash()
    test_source_archive()
    print(f"{info['product_name']} {VERSION} package integrity and privacy checks passed")
    return 0
if __name__ == '__main__':
    sys.exit(main())
