#!/usr/bin/env python3
"""Create a separate public source tree with original, generated sample artwork.

Only the standard library is used. This never overwrites the private checkout,
initializes Git, publishes files, or imports application/runtime data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ORIGINAL_ZIP_SHA256 = 'a054b5c4494ffd5bb2a01e0a156f23211b8ec15c4f8a0df44959000089e31d0f'
ORIGINAL_SOURCE_COMMIT = '26305f3250c1fc34ab4a190a4ec429ab705a79f8'
ROOT_FILES = {
    'AGENTS.md', 'README.md', 'NOTICE.md', 'VERSION', 'SOURCE_REVISION.json',
    'requirements.txt', 'requirements-dev.txt', 'requirements-assets.txt', 'pyproject.toml', 'package.json',
    'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'next.config.ts',
    'drizzle.config.ts', 'cloudflare-env.d.ts', 'raw-imports.d.ts', '.gitignore',
    '.dockerignore', '.gitattributes', '.env.example', 'Dockerfile', 'Dockerfile.frontend', 'compose.dev.yaml',
}
SOURCE_DIRS = {'app', 'apps', 'packages', 'edge', 'static', 'preview', 'db', 'drizzle', 'scripts', 'build', 'lib', 'tools', 'tests', 'deploy', '.github'}
SOURCE_SUFFIXES = {'.py', '.js', '.cjs', '.mjs', '.ts', '.tsx', '.sql', '.css', '.html', '.sh', '.yaml', '.yml', '.toml', '.md', '.LICENSE'}
DOC_FILES = {
    'API_CONTRACT.md', 'ARCHITECTURE.md', 'RUNBOOK.md', 'DEMO.md', 'OPEN_SOURCE.md',
    'SELF_HOSTED.md', 'VISUAL_GUIDE.md', 'REQUIREMENTS_V06.md', 'REQUIREMENTS_V05.md',
    'CHANGELOG.md', 'SITES_ITERATION.md', 'acceptance.json',
    'openapi.json',
}
DEPLOY_TEMPLATES = {'deploy/onstage.service', 'deploy/Caddyfile.snippet', 'deploy/nginx-site.conf.example'}
EXCLUDED_PARTS = {'__pycache__', '.pytest_cache', 'node_modules', 'private', 'backups', 'data', '.git', '.openai', 'vendor', 'reports'}
MARKER = 'onstage-public-source-v1'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def sample_png(width: int, height: int, key: str) -> bytes:
    """Original color-block artwork, with a contrasting central star; no input pixels."""
    seed = hashlib.sha256(key.encode()).digest()
    base = tuple(40 + value % 130 for value in seed[:3])
    light = tuple(min(255, value + 65) for value in base)
    rows = bytearray()
    for y in range(height):
        row = bytearray()
        band = y // max(1, height // 5)
        for tile in range(5):
            left, right = width * tile // 5, width * (tile + 1) // 5
            rgb = light if (tile + band) % 2 else base
            row.extend(bytes((*rgb, 255)) * (right - left))
        ny = abs(2 * y / max(1, height - 1) - 1)
        extent = max(.13 if ny < .60 else 0, .60 if ny < .13 else 0, .48 - ny)
        if extent > 0:
            left, right = int((1 - extent) * width / 2), int((1 + extent) * width / 2)
            row[left * 4:right * 4] = bytes((250, 246, 220, 255)) * (right - left)
        rows.append(0)
        rows.extend(row)
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack('!I', len(payload)) + kind + payload + struct.pack('!I', zlib.crc32(kind + payload) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + chunk(b'IEND', b'')


def sample_svg(width: int, height: int, key: str) -> bytes:
    color = hashlib.sha256(key.encode()).hexdigest()[:6]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 320 240" role="img" aria-label="Original sample artwork">'
            f'<rect width="320" height="240" fill="#{color}"/><path d="M160 35 178 96 240 120 178 144 160 205 142 144 80 120 142 96Z" fill="#fff6dc"/>'
            '<text x="160" y="225" text-anchor="middle" font-family="sans-serif" font-size="15" fill="white">ONSTAGE SAMPLE</text></svg>\n').encode()


def safe_relative(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or '\\' in raw or any(part in {'..', '.', ''} for part in raw.split('/')):
        raise ValueError(f'Unsafe relative asset path: {raw}')
    if not raw.startswith('static/assets/'):
        raise ValueError(f'Asset outside static/assets: {raw}')
    return path


def reject_symlink_path(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Symlink paths are not allowed: {relative}')


def allowed_source(path: Path) -> bool:
    parts = path.parts
    if any(part in EXCLUDED_PARTS or part.startswith('.env') for part in parts):
        return path.as_posix() == '.env.example'
    if len(parts) == 1:
        return path.name in ROOT_FILES
    if parts[0] == 'docs':
        return len(parts) == 2 and path.name in DOC_FILES
    if path.as_posix() in DEPLOY_TEMPLATES:
        return True
    if parts[0] not in SOURCE_DIRS or (parts[:2] == ('static', 'assets')):
        return False
    if parts[:2] in {('build','web'), ('build','admin')}:
        return False
    if parts[0] == 'apps' and path.suffix == '.json':
        return True
    if parts[:2] == ('deploy','frontend') and path.suffix == '.template':
        return True
    if path.as_posix() == 'deploy/monitoring/onstage-dashboard.json':
        return True
    if path.as_posix() == 'static/onstage-mark.svg' or path.as_posix() == 'app/catalog.json':
        return True
    if path.as_posix() == 'static/manifest.webmanifest' or (parts[:2] == ('static', 'icons') and path.suffix == '.png'):
        return True
    if parts[:2] == ('drizzle', 'meta') and path.suffix == '.json':
        return True
    return path.suffix in SOURCE_SUFFIXES or path.name.endswith('.LICENSE')


def git_revision(source: Path) -> str | None:
    try:
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def export_source(source: Path, output: Path) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('Export must be outside the private source tree and cannot contain it.')
    if output.exists():
        raise ValueError('Output must not already exist; existing files are never overwritten.')
    origin = json.loads((source / 'SOURCE_REVISION.json').read_text(encoding='utf-8'))
    if origin.get('source_commit') != ORIGINAL_SOURCE_COMMIT:
        raise ValueError('Unexpected original source revision; verify provenance before exporting.')
    manifest_bytes = (source / 'docs/ASSET_MANIFEST.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    original_assets = manifest['assets']
    assets = {item['path']: item for item in original_assets}
    runtime_assets = {path.relative_to(source).as_posix() for path in (source / 'static/assets').rglob('*') if path.is_file()}
    derived = source / 'static/assets-manifest.json'
    variants = set()
    if derived.is_file():
        from tools.optimize_assets import verify
        verify(source)
        variants = {item['url'].lstrip('/') for item in json.loads(derived.read_text(encoding='utf8'))['assets'].values()}
    if runtime_assets != set(assets) | variants:
        raise ValueError('Every runtime asset must be identified in ASSET_MANIFEST before public export.')
    for raw in assets:
        rel = safe_relative(raw)
        item = assets[raw]
        original = source / rel
        if original.is_symlink() or not original.is_file() or digest(original.read_bytes()) != item['sha256']:
            raise ValueError(f'Original asset identity mismatch: {raw}')
        if original.suffix.lower() not in {'.png', '.webp'}:
            raise ValueError(f'Unsupported asset format: {raw}')
    # Validate the whole copy set before creating anything.
    source_files = sorted(path for path in source.rglob('*') if path.is_file() and allowed_source(path.relative_to(source)))
    for path in source_files:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source.parent):
            raise ValueError(f'Symlinks cannot enter public export: {path.relative_to(source)}')
    output.mkdir(parents=True)
    for path in source_files:
        destination = output / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    remap = {raw: str(Path(raw).with_suffix('.svg')).replace('\\', '/') for raw in assets if raw.endswith('.webp')}
    generated = []
    for raw, old in sorted(assets.items()):
        rel = remap.get(raw, raw)
        width, height = int(old['width']), int(old['height'])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise ValueError(f'Unexpected asset dimensions: {raw}')
        content = sample_svg(width, height, raw) if rel.endswith('.svg') else sample_png(width, height, raw)
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        generated.append({'path': rel, 'web_url': '/' + rel, 'source_type': 'project_original_generated_sample',
                          'author': 'Onstage sample generator', 'processing': 'Original geometric sample; no source image pixels copied',
                          'width': width, 'height': height, 'bytes': len(content), 'sha256': digest(content),
                          'original_asset_path': raw, 'original_asset_sha256': old['sha256']})
    # Rewrite only textual source references. Stable character/scenario/sticker IDs do not change.
    for path in output.rglob('*'):
        if not path.is_file() or path.name == 'SOURCE_REVISION.json' or path.suffix == '.png':
            continue
        text = path.read_text(encoding='utf-8')
        for before, after in remap.items():
            text = text.replace(before, after)
        for key, replacement in [('public_url', 'https://onstage.example.org'), ('site_project_id', 'ONSTAGE_SITE_PROJECT_ID'), ('site_version_id', 'ONSTAGE_SITE_VERSION_ID'), ('deployment_id', 'ONSTAGE_DEPLOYMENT_ID')]:
            if origin.get(key):
                text = text.replace(str(origin[key]), replacement)
        # The fallback character/scene paths are template literals rather than catalog literals.
        if path.relative_to(output).as_posix() == 'static/app.js':
            text = text.replace('}.webp`', '}.svg`')
        path.write_text(text, encoding='utf-8', newline='\n')
    # A sample-only export must never point back at the original derived bitmaps.
    write_json(output / 'static/assets-manifest.json', {'schema_version': 1, 'assets': {}, 'mode': 'sample-only-original-urls'})
    write_json(output / 'docs/ASSET_MANIFEST.json', {
        'format': 'onstage-public-sample-assets-v1', 'all_runtime_assets_same_origin': True,
        'asset_count': len(generated), 'assets': generated,
        'licensing_note': 'These are original generated geometric samples. No third-party source pixels are distributed; code/asset licensing remains separately specified by the maintainer.',
    })
    provenance = {
        'format': MARKER, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'original_source_commit': ORIGINAL_SOURCE_COMMIT, 'original_zip_sha256': ORIGINAL_ZIP_SHA256,
        'private_checkout_revision': git_revision(source), 'private_checkout_may_include_uncommitted_changes': True,
        'inventory_scope': 'PUBLIC_SOURCE_FILES.json records the initial export only; it does not identify a later Git commit or require refreshing hashes after ordinary source edits. Git records subsequent changes.',
        'source_revision_sha256': digest((source / 'SOURCE_REVISION.json').read_bytes()),
        'original_asset_manifest_sha256': digest(manifest_bytes),
        'original_asset_manifest': manifest,
        'sanitization': ['Explicit source allowlist; no original Git history', 'No reports, instance hosting config, vendor packages, databases, logs, private exports, caches or ZIPs',
                         'All bitmap artwork replaced by original generated geometric samples; PNG paths preserved; WebP references remapped to SVG',
                         'Original SOURCE_REVISION retained as historical source provenance, not evidence for this public sample build'],
        'path_remap': remap,
        'original_assets': [{'path': item['path'], 'sha256': item['sha256'], 'source_type': item.get('source_type')} for item in original_assets],
    }
    write_json(output / 'PUBLIC_SOURCE_PROVENANCE.json', provenance)
    if not (output / 'docs/acceptance.json').exists():
        write_json(output / 'docs/acceptance.json', {'project': 'Onstage public sample source', 'overall_status': 'NOT VERIFIED',
            'scope': 'Generated public sample source; run current tests and record results for the published commit.'})
    (output / 'docs/CONTENT_SOURCES.md').write_text('# Public sample artwork\n\nAll bundled bitmaps are original geometric samples generated by `tools/prepare_public_source.py`. Stable business IDs and PNG paths are preserved. WebP paths are mapped to SVG in PUBLIC_SOURCE_PROVENANCE.json. Original asset hashes identify excluded material; they do not describe bundled sample pixels.\n', encoding='utf-8')
    note = '''\n\n## Public sample content\n\nThis public source tree contains original generated geometric sample artwork, not the original character, band, sticker or backdrop images. Business IDs and PNG paths remain stable; WebP paths are remapped to same-name SVGs. `docs/ASSET_MANIFEST.json` describes the bundled sample bytes; `PUBLIC_SOURCE_PROVENANCE.json` records excluded source identities and the original ZIP/source commit. Original `SOURCE_REVISION.json` is historical provenance, not a verification result for this tree. No code license or third-party artwork redistribution permission is implied.\n\nTo restore assets **locally only**, obtain the original package independently and unpack it to a separate private directory. Run `python tools/prepare_public_source.py restore-assets --source PATH_TO_PRIVATE_ORIGINAL --target PATH_TO_LOCAL_PUBLIC_CHECKOUT`. The command verifies all original asset hashes before restoring them, and refuses to modify a non-sample target. This changes the local content files and manifest; do not commit, push, package or redistribute the restored images without the appropriate rights. Keep the public sample branch clean, or use a separate ignored local copy for demonstrations.\n'''
    for name in ('README.md', 'NOTICE.md'):
        path = output / name
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(note)
    files = [{'path': path.relative_to(output).as_posix(), 'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())}
             for path in sorted(output.rglob('*')) if path.is_file()]
    write_json(output / 'PUBLIC_SOURCE_FILES.json', {'format': MARKER, 'identity_scope': 'initial-export-only', 'files': files, 'note': 'Initial export identity snapshot, excluding itself. Not a rolling integrity manifest or current Git commit identity. Subsequent source changes are tracked by Git; current bundled asset identities remain in docs/ASSET_MANIFEST.json.'})
    return {'output': str(output), 'files': len(files) + 1, 'sample_assets': len(generated), 'original_source_commit': ORIGINAL_SOURCE_COMMIT}


def restore_assets(source: Path, target: Path) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Original source and public target must be separate directories.')
    provenance = json.loads((target / 'PUBLIC_SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    if provenance.get('format') != MARKER:
        raise ValueError('Target is not a generated public sample tree.')
    assets = provenance['original_assets']
    for item in assets:
        original = source / safe_relative(item['path'])
        reject_symlink_path(original, source)
        reject_symlink_path(target / item['path'], target)
        mapped = provenance['path_remap'].get(item['path'])
        if mapped:
            reject_symlink_path(target / safe_relative(mapped), target)
        if original.is_symlink() or digest(original.read_bytes()) != item['sha256']:
            raise ValueError(f'Original asset identity mismatch: {item["path"]}')
    if digest((source / 'docs/ASSET_MANIFEST.json').read_bytes()) != provenance['original_asset_manifest_sha256']:
        raise ValueError('Original asset manifest identity mismatch.')
    remap = provenance['path_remap']
    for path in target.rglob('*'):
        if not path.is_file() or not allowed_source(path.relative_to(target)) or path.suffix in {'.png', '.svg', '.webp'} or path.name in {'PUBLIC_SOURCE_PROVENANCE.json', 'PUBLIC_SOURCE_FILES.json', 'SOURCE_REVISION.json'}:
            continue
        text = path.read_text(encoding='utf-8')
        for before, after in remap.items():
            text = text.replace(after, before)
        if path.relative_to(target).as_posix() == 'static/app.js':
            text = text.replace('}.svg`', '}.webp`')
        path.write_text(text, encoding='utf-8', newline='\n')
    for item in assets:
        original = source / item['path']
        shutil.copyfile(original, target / item['path'])
        mapped = remap.get(item['path'])
        if mapped:
            (target / mapped).unlink()
    shutil.copyfile(source / 'docs/ASSET_MANIFEST.json', target / 'docs/ASSET_MANIFEST.json')
    write_json(target / 'LOCAL_ASSETS_RESTORED.json', {'warning': 'Private local artwork restored. Do not commit or redistribute without appropriate rights.', 'assets': len(assets)})
    return {'target': str(target), 'restored_assets': len(assets), 'public_commit_allowed': False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--source', type=Path, default=ROOT)
    export.add_argument('--output', type=Path, required=True)
    restore = sub.add_parser('restore-assets')
    restore.add_argument('--source', type=Path, required=True)
    restore.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export_source(args.source, args.output) if args.command == 'export' else restore_assets(args.source, args.target)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'Public source preparation failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
