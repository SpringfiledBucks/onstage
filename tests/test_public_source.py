"""Sample export isolation and complete original-artwork deployment acceptance."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib

import pytest

from tools.prepare_public_source import ROOT, export_source, restore_assets


@pytest.fixture(scope='module')
def public_tree(tmp_path_factory):
    provenance = ROOT / 'PUBLIC_SOURCE_PROVENANCE.json'
    if provenance.is_file() and json.loads(provenance.read_text(encoding='utf8')).get('content_mode') != 'original-artwork':
        return ROOT
    destination = tmp_path_factory.mktemp('public-export') / 'source'
    result = export_source(ROOT, destination)
    assert result['sample_assets'] > 0
    return destination


def test_public_export_preserves_catalog_ids_and_excludes_private_material(public_tree):
    source_catalog = json.loads((ROOT / 'app/catalog.json').read_text(encoding='utf-8'))
    public_catalog = json.loads((public_tree / 'app/catalog.json').read_text(encoding='utf-8'))
    for group in ('bands', 'characters', 'scenarios', 'stickers'):
        assert [item['id'] for item in source_catalog[group]] == [item['id'] for item in public_catalog[group]]
    assert (public_tree / 'SOURCE_REVISION.json').read_bytes() == (ROOT / 'SOURCE_REVISION.json').read_bytes()
    inventory = json.loads((public_tree / 'PUBLIC_SOURCE_FILES.json').read_text(encoding='utf-8'))
    exported = [Path(item['path']) for item in inventory['files']]
    for excluded in ('.git', '.openai', 'reports', 'vendor', 'node_modules', 'data', 'SHA256SUMS'):
        assert not any(path.parts[0] == excluded for path in exported)
    assert not any(path.suffix in {'.zip', '.sqlite3', '.log', '.pyc'} for path in exported)
    assert not (public_tree / 'docs/CODEX_HANDOFF.md').exists()
    provenance = json.loads((public_tree / 'PUBLIC_SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    assert provenance['original_source_commit'] == '26305f3250c1fc34ab4a190a4ec429ab705a79f8'
    assert provenance['original_zip_sha256'] == 'a054b5c4494ffd5bb2a01e0a156f23211b8ec15c4f8a0df44959000089e31d0f'
    assert json.loads((public_tree / 'docs/acceptance.json').read_text(encoding='utf-8'))['overall_status'] == json.loads((ROOT / 'docs/acceptance.json').read_text(encoding='utf-8'))['overall_status']


def test_bundled_samples_are_valid_original_images_with_matching_manifest(public_tree):
    original = json.loads((ROOT / 'docs/ASSET_MANIFEST.json').read_text(encoding='utf-8'))
    source_provenance = ROOT / 'PUBLIC_SOURCE_PROVENANCE.json'
    excluded_assets = json.loads(source_provenance.read_text(encoding='utf-8'))['original_assets'] if source_provenance.is_file() else original['assets']
    old_hashes = {item['sha256'] for item in excluded_assets}
    manifest = json.loads((public_tree / 'docs/ASSET_MANIFEST.json').read_text(encoding='utf-8'))
    assert len(manifest['assets']) == len(original['assets'])
    for item in manifest['assets']:
        image = public_tree / item['path']
        data = image.read_bytes()
        assert hashlib.sha256(data).hexdigest() == item['sha256']
        assert item['sha256'] not in old_hashes
        assert item['source_type'] == 'project_original_generated_sample'
        if image.suffix == '.png':
            assert data[:8] == b'\x89PNG\r\n\x1a\n'
            width, height = struct.unpack('!II', data[16:24])
            assert (width, height) == (item['width'], item['height'])
            chunks, offset = [], 8
            while offset < len(data):
                length = struct.unpack('!I', data[offset:offset+4])[0]
                kind, value = data[offset+4:offset+8], data[offset+8:offset+8+length]
                assert zlib.crc32(kind + value) & 0xffffffff == struct.unpack('!I', data[offset+8+length:offset+12+length])[0]
                if kind == b'IDAT': chunks.append(value)
                offset += 12 + length
            assert len(zlib.decompress(b''.join(chunks))) == height * (width * 4 + 1)
        else:
            assert image.suffix == '.svg'
            svg = ET.fromstring(data)
            assert svg.tag == '{http://www.w3.org/2000/svg}svg'
            assert 'ONSTAGE SAMPLE' in ''.join(svg.itertext())
    catalog = json.loads((public_tree / 'app/catalog.json').read_text(encoding='utf-8'))
    for group in ('bands', 'characters', 'scenarios', 'stickers'):
        for item in catalog[group]:
            for key in ('image', 'image_url', 'avatar_image', 'detail_image'):
                if item.get(key):
                    assert (public_tree / item[key].lstrip('/')).is_file(), item[key]
    inventory = json.loads((public_tree / 'PUBLIC_SOURCE_FILES.json').read_text(encoding='utf-8'))
    assert inventory['format'] == 'onstage-public-source-v1'
    assert inventory['identity_scope'] == 'initial-export-only'
    assert all(set(item) == {'path', 'bytes', 'sha256'} and len(item['sha256']) == 64 and item['bytes'] >= 0 for item in inventory['files'])
    source_record = next(item for item in inventory['files'] if item['path'] == 'SOURCE_REVISION.json')
    assert source_record['sha256'] == hashlib.sha256((public_tree / 'SOURCE_REVISION.json').read_bytes()).hexdigest()


def test_exported_application_and_deployment_preparation_are_runnable(public_tree, tmp_path, http_database):
    runtime_tree = ROOT if (ROOT / 'PUBLIC_SOURCE_PROVENANCE.json').is_file() else public_tree
    env = {**os.environ, 'ONSTAGE_DATA_DIR': str(tmp_path / 'data'), 'ONSTAGE_PUBLIC_ORIGIN': 'http://testserver', 'ONSTAGE_SECURE_COOKIE': '0'}
    env['DATABASE_URL'] = http_database.settings.database_url
    env.pop('ONSTAGE_DATABASE_URL', None)
    script = '''
import json, hashlib
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from tools.prepare_deployment import prepare
from tempfile import TemporaryDirectory
with TemporaryDirectory(prefix='onstage-public-proxies-') as directory:
    root=Path(directory)
    nginx=prepare('https://sample.example.org', 'nginx', root/'nginx', tls_cert='/etc/sample/fullchain.pem', tls_key='/etc/sample/privkey.pem')
    caddy=prepare('https://sample.example.org', 'caddy', root/'caddy')
    assert not nginx['deployment_executed'] and not caddy['deployment_executed']
    assert 'server_name sample.example.org;' in (root/'nginx/nginx-onstage.conf').read_text()
    assert 'sample.example.org {' in (root/'caddy/Caddyfile.onstage.snippet').read_text()
    for proxy in ('nginx','caddy'):
        assert (root/proxy/'onstage.service').is_file()
        assert 'ONSTAGE_PUBLIC_ORIGIN=https://sample.example.org' in (root/proxy/'onstage.env').read_text()
with TestClient(app) as client:
    assert client.get('/readyz').status_code == 200
    assert client.post('/api/session', json={'nickname':'Sample test'}).status_code == 200
    response=client.get('/api/catalog'); assert response.status_code == 200
    catalog=response.json(); checked=set()
    for group in ('bands','characters','scenarios','stickers'):
        for item in catalog[group]:
            for key in ('image','image_url','avatar_image','detail_image'):
                if item.get(key): checked.add(item[key])
    for url in checked:
        image=client.get(url); assert image.status_code == 200, url
        assert image.headers['content-type'].startswith('image/'), url
    manifest=json.loads(Path('docs/ASSET_MANIFEST.json').read_text(encoding='utf8'))
    for item in manifest['assets']:
        image=client.get('/'+item['path']); assert image.status_code == 200, item['path']
        assert image.headers['content-type'].startswith('image/'), item['path']
        assert hashlib.sha256(image.content).hexdigest() == item['sha256'], item['path']
    provenance=json.loads(Path('PUBLIC_SOURCE_PROVENANCE.json').read_text(encoding='utf8'))
    if provenance.get('content_mode') == 'original-artwork':
        expected={item['path']:item['sha256'] for item in provenance['original_assets']}
        assert {item['path']:item['sha256'] for item in manifest['assets']} == expected
        assert len(expected) == 185
        from tools.optimize_assets import verify
        verify(Path.cwd())
        delivery=json.loads(Path('static/assets-manifest.json').read_text(encoding='utf8'))
        variants={item['url'].lstrip('/') for item in delivery['assets'].values()}
        assert set(expected) | variants == {f.as_posix() for f in Path('static/assets').rglob('*') if f.is_file()}
    assert client.get('/').status_code == 200
    print(json.dumps({'images_served':len(checked),'manifest_assets_verified':len(manifest['assets']),'characters':len(catalog['characters'])}))
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=runtime_tree, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])['characters'] == 10


def test_export_rejects_overwrite_and_unsafe_source_assets(public_tree, tmp_path):
    existing = tmp_path / 'existing-export'
    existing.mkdir()
    with pytest.raises(ValueError, match='already exist'):
        export_source(ROOT, existing)
    with pytest.raises(ValueError, match='outside'):
        export_source(ROOT, ROOT / 'nested-public-export')
    source = tmp_path / 'private'
    (source / 'docs').mkdir(parents=True)
    (source / 'static/assets').mkdir(parents=True)
    (source / 'SOURCE_REVISION.json').write_text(json.dumps({'source_commit':'26305f3250c1fc34ab4a190a4ec429ab705a79f8'}))
    (source / 'docs/ASSET_MANIFEST.json').write_text(json.dumps({'assets':[]}))
    (source / 'static/assets/unclassified.png').write_bytes(b'Private image not in manifest')
    with pytest.raises(ValueError, match='identified'):
        export_source(source, tmp_path / 'must-not-exist')
    assert not (tmp_path / 'must-not-exist').exists()


def test_local_restore_verifies_before_writing(public_tree, tmp_path):
    # Copy the complete sample tree into a throwaway local workspace, never modify the fixture/private source.
    import shutil
    target = tmp_path / 'local-demo'
    shutil.copytree(public_tree, target)
    before = (target / 'app/catalog.json').read_bytes()
    wrong = tmp_path / 'wrong-original'
    wrong.mkdir()
    with pytest.raises(OSError):
        restore_assets(wrong, target)
    assert (target / 'app/catalog.json').read_bytes() == before
    provenance = ROOT / 'PUBLIC_SOURCE_PROVENANCE.json'
    if provenance.is_file() and json.loads(provenance.read_text(encoding='utf8')).get('content_mode') != 'original-artwork':
        return  # Public CI proves failure is non-mutating; the private checkout also proves full restoration.
    result = restore_assets(ROOT, target)
    assert result['public_commit_allowed'] is False
    assert (target / 'app/catalog.json').read_bytes() == (ROOT / 'app/catalog.json').read_bytes()
    original = json.loads((ROOT / 'docs/ASSET_MANIFEST.json').read_text(encoding='utf-8'))
    for item in original['assets']:
        assert hashlib.sha256((target / item['path']).read_bytes()).hexdigest() == item['sha256']
    assert (target / 'LOCAL_ASSETS_RESTORED.json').is_file()
