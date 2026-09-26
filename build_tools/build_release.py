"""Build the executable package and a separate complete source archive."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_files import SOURCE_FILES, DATA_FILES, VERSION, assert_plain_path, package_file_mapping, source_archive_files, release_metadata

def run(arguments: list[str], cwd: Path) -> None:
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    result = subprocess.run([sys.executable, '-B', *arguments], cwd=cwd, env=environment, creationflags=int(getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    if result.returncode:
        raise RuntimeError('Build step failed: ' + str(arguments[0]))

def build(source_root: Path, output_directory: Path, version: str) -> dict:
    source_root = source_root.resolve()
    if version != VERSION:
        raise ValueError('Version differs from the source version')
    info = json.loads((source_root / 'release-info.json').read_text(encoding='utf-8'))
    if info != release_metadata():
        raise ValueError('Release metadata differs from the product source')
    output_directory = output_directory if output_directory.is_absolute() else source_root / output_directory
    output_directory = assert_plain_path(output_directory.absolute(), output_directory.absolute().parent)
    package_name = info['package_name']
    exe_name = Path(info['executable']).stem
    if any((Path(value).name != value or ':' in value for value in [package_name, exe_name])):
        raise ValueError('Invalid release filename')
    final = assert_plain_path(output_directory / package_name, output_directory)
    archive_path = output_directory / (package_name + '.zip')
    hash_path = output_directory / (package_name + '.zip.sha256')
    source_archive_path = output_directory / (package_name + '-原始碼.zip')
    source_hash_path = output_directory / (package_name + '-原始碼.zip.sha256')
    outputs = [path for path in (final, archive_path, hash_path, source_archive_path, source_hash_path) if path is not None]
    if any((path.exists() for path in outputs)):
        raise FileExistsError('Release output already exists; select a new output directory')
    for relative in SOURCE_FILES | DATA_FILES:
        path = assert_plain_path(source_root / relative, source_root)
        if not path.is_file():
            raise FileNotFoundError(relative)
    source_archive_files(source_root)
    for script in ['build_reviewed_status_catalog.py', 'build_runtime_status_index.py', 'build_catalog_manifest.py']:
        run([str(source_root / 'build_tools' / script)], source_root)
    build_root = assert_plain_path(source_root / 'build' / ('release-' + uuid.uuid4().hex[:12]), source_root)
    build_root.mkdir(parents=True)
    output_directory.mkdir(parents=True, exist_ok=True)
    hidden = [str(Path(relative).with_suffix('')).replace('\\', '.').replace('/', '.') for relative in SOURCE_FILES if relative.startswith(('monitor_core/', 'monitor_ui/', 'catalog/', 'app/')) and relative.endswith('.py') and (not relative.endswith('__init__.py'))]
    arguments = ['-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir', '--icon', str(source_root / 'assets/app.ico'), '--name', exe_name, '--distpath', str(build_root / 'dist'), '--workpath', str(build_root / 'work'), '--specpath', str(build_root / 'spec'), '--paths', str(source_root), '--additional-hooks-dir', str(source_root / 'pyinstaller_hooks')]
    for name in ['tkinter', 'tkinter.ttk', 'tkinter.filedialog', *sorted(hidden)]:
        arguments += ['--hidden-import', name]
    arguments.append(str(source_root / 'rrf_monitor.py'))
    run(arguments, source_root)
    runtime = build_root / 'dist' / exe_name
    if not (runtime / info['executable']).is_file():
        raise FileNotFoundError('Compiled executable missing')
    for path in runtime.rglob('*'):
        assert_plain_path(path, build_root)
    shutil.copytree(runtime, final)
    for relative, source_relative in sorted(package_file_mapping().items()):
        target = final / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / source_relative, target)
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(final.rglob('*')):
            if path.is_file():
                archive.write(path, package_name + '/' + path.relative_to(final).as_posix())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    hash_path.write_text(digest + '\n', encoding='ascii')
    with zipfile.ZipFile(source_archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(source_archive_files(source_root)):
            path = assert_plain_path(source_root / relative, source_root)
            archive.write(path, package_name + '-原始碼/' + relative)
    source_digest = hashlib.sha256(source_archive_path.read_bytes()).hexdigest()
    source_hash_path.write_text(source_digest + '\n', encoding='ascii')
    try:
        run([str(source_root / 'build_tools/check_package.py'), '--source-root', str(source_root), '--output-directory', str(output_directory)], source_root)
    except Exception:
        suffix = '.failed-' + uuid.uuid4().hex[:8]
        for path in outputs:
            assert_plain_path(path, output_directory)
            if path.exists():
                path.rename(path.with_name(path.name + suffix))
        raise
    result = {'product_name': info['product_name'], 'version': VERSION, 'package_directory': str(final), 'zip': str(archive_path), 'sha256': digest, 'zip_bytes': archive_path.stat().st_size, 'source_root': str(source_root)}
    result.update(source_zip=str(source_archive_path), source_zip_sha256=source_digest, source_zip_bytes=source_archive_path.stat().st_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-directory', type=Path, default=Path('dist'))
    parser.add_argument('--version', default=VERSION)
    args = parser.parse_args()
    build(args.source_root, args.output_directory, args.version)
if __name__ == '__main__':
    main()
