"""Explicit portable source/data allowlists shared by staging and package checks."""
from pathlib import Path
VERSION = '1.6.5'
SOURCE_FILES = {
    '.gitattributes',
    '.gitignore',
    'DATA_SOURCES.md',
    'README.md',
    'app/__init__.py',
    'app/bootstrap.py',
    'app/lifecycle.py',
    'build_tools/build_catalog_manifest.py',
    'build_tools/build_release.py',
    'build_tools/build_runtime_status_index.py',
    'build_tools/check_package.py',
    'build_tools/release_files.py',
    'catalog/__init__.py',
    'catalog/development_store.py',
    'catalog/pets.py',
    'catalog/repository.py',
    'catalog/schema.py',
    'catalog/sync_worker.py',
    'catalog/unknown_journal.py',
    'monitor_core/__init__.py',
    'monitor_core/actor_state.py',
    'monitor_core/alert_policies.py',
    'monitor_core/alerts.py',
    'monitor_core/capabilities.py',
    'monitor_core/catalog.py',
    'monitor_core/pet_alerts.py',
    'monitor_core/pet_snapshot.py',
    'monitor_core/policies.py',
    'monitor_core/replay_discovery.py',
    'monitor_core/session.py',
    'monitor_core/snapshots.py',
    'monitor_ui/__init__.py',
    'monitor_ui/card_layout.py',
    'monitor_ui/pet_overlay.py',
    'monitor_ui/view_models.py',
    'pyinstaller_hooks/hook-_tkinter.py',
    'pyinstaller_hooks/pre_find_module_path/hook-tkinter.py',
    'release-info.json',
    'rrf_monitor.py',
    'tests/test_final_core_regressions.py',
    'tests/test_final_settings_regressions.py',
    'tests/test_pet_alerts.py',
    'tests/test_pet_catalog.py',
    'tests/test_pet_feature_integration.py',
    'tests/test_pet_snapshot.py',
    'tests/test_scope_boundaries.py',
    '使用說明.txt',
    '啟動監控器.bat',
    '啟動監控器.vbs',
    '打包版本.bat',
    '打包版本.ps1',
}
DATA_FILES = {'data/' + name for name in ('EFSTIDs.lua', 'stateiconinfo.lua', 'runtime_status_index.json', 'catalog_manifest.json', 'status_catalog.json', 'client_catalog.json', 'status_classification.json', 'pet_catalog.json', 'job_status_index.json', 'job_skill_catalog.json', '可擴充資料庫.json')}

def package_file_mapping() -> dict[str, str]:
    """Map executable-package data to its verified source files."""
    return {**{name: name for name in DATA_FILES}, '使用說明.txt': '使用說明.txt', 'data/資料來源.txt': 'DATA_SOURCES.md'}

def source_archive_files(root: Path) -> set[str]:
    """Require complete buildable source, data and synthetic tests."""
    files = SOURCE_FILES | DATA_FILES
    for name in files:
        if not assert_plain_path(root / name, root).is_file():
            raise FileNotFoundError(name)
    return files

def assert_safe_archive_parts(parts: list[str]) -> None:
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$'} | {prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³'}
    for part in parts:
        if not part or part in {'.', '..'} or part != part.rstrip(' .') or any((ord(char) < 32 or char in '<>:"|?*\\' for char in part)) or (part.split('.', 1)[0].upper() in reserved):
            raise ValueError('Unsafe archive path component')

def assert_plain_path(path: Path, root: Path) -> Path:
    root = root.resolve()
    absolute = path.absolute()
    if absolute == root or not absolute.is_relative_to(root):
        raise ValueError('Path must be a child of the intended workspace')
    for current in (absolute, *absolute.parents):
        if current == root.parent:
            break
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Symbolic links and junctions are not allowed')
    if not absolute.resolve().is_relative_to(root):
        raise ValueError('Resolved path leaves the intended workspace')
    return absolute

def release_metadata() -> dict[str, str]:
    """The names and version shared by the builder and integrity checker."""
    return {'product_name': 'RO-RRF即時狀態監控器', 'version': VERSION, 'executable': 'RO-RRF即時狀態監控器.exe', 'package_name': 'RO-RRF即時狀態監控器-v' + VERSION, 'source_distribution': 'source'}
