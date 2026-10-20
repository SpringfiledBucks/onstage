"""Actual ASGI HTTP policies: public immutable artwork, private API and text gzip."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_public_cache_private_boundaries_and_gzip(tmp_path, http_database):
    app = create_app(data_dir=tmp_path, public_origin='http://testserver', secure_cookie=False)
    manifest = json.loads((ROOT / 'static/assets-manifest.json').read_text(encoding='utf8'))
    source, entry = next(iter(manifest['assets'].items()))
    with TestClient(app, headers={'Accept-Encoding':'gzip'}) as client:
        image = client.get(entry['url'])
        assert image.status_code == 200
        assert image.headers['cache-control'] == 'public, max-age=31536000, immutable'
        assert 'content-encoding' not in image.headers
        assert len(image.content) == entry['bytes']
        warm = client.get(entry['url'], headers={'If-None-Match':image.headers['etag']})
        assert warm.status_code == 304 and warm.content == b''
        assert 'immutable' in warm.headers['cache-control']
        legacy = client.get(source)
        assert legacy.status_code == 200 and legacy.headers['cache-control'] == 'no-cache'
        script = client.get('/static/app.js')
        assert script.headers['content-encoding'] == 'gzip'
        assert 'Accept-Encoding' in script.headers['vary']
        assert script.headers['cache-control'] == 'no-cache'
        assert client.get('/api/rooms').headers['cache-control'] == 'no-store'
        session = client.post('/api/session', json={'nickname':'cache test'})
        assert session.headers['cache-control'] == 'no-store'
        assert client.get('/api/catalog').headers['cache-control'] == 'no-store'
        missing = client.get('/static/assets/optimized/'+'0'*64+'.webp')
        assert missing.status_code == 404 and missing.headers['cache-control'] == 'no-store'
        sw = client.get('/sw.js')
        assert sw.status_code == 200 and sw.headers['service-worker-allowed'] == '/'
        assert sw.headers['cache-control'] == 'no-cache'
        webmanifest = client.get('/static/manifest.webmanifest')
        assert webmanifest.status_code == 200 and webmanifest.json()['scope'] == '/'
