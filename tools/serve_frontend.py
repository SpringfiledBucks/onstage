"""Loopback-only frontend development server; production uses Dockerfile.frontend."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit


def main():
    # Windows registry MIME tables can omit WebP; SW verifies MIME before caching.
    mimetypes.add_type('image/webp', '.webp')
    mimetypes.add_type('application/manifest+json', '.webmanifest')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--port', type=int, default=8840)
    parser.add_argument('--api-origin', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    origin = urlsplit(args.api_origin)
    if origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        parser.error('API address must be an exact non-secret HTTP(S) Origin')
    root = args.directory.resolve(strict=True)
    api = args.api_origin
    ws = ('wss' if origin.scheme == 'https' else 'ws') + '://' + origin.netloc

    class Handler(SimpleHTTPRequestHandler):
        def send_response(self, code, message=None):
            self._onstage_status = code
            super().send_response(code, message)

        def log_message(self, *_):
            pass

        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Service-Worker-Allowed', '/')
            immutable = getattr(self, '_onstage_status', 0) in {200, 304} and ('/static/assets/optimized/' in self.path or self.path.startswith('/assets/'))
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable' if immutable else 'no-store')
            super().end_headers()

        def do_GET(self):
            if urlsplit(self.path).path == '/config.js':
                body = ('window.ONSTAGE_CONFIG = ' + json.dumps({'apiBaseUrl': api, 'wsBaseUrl': ws}) + ';\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not Path(self.translate_path(self.path)).is_file() and not Path(self.translate_path(self.path)).is_dir():
                if not urlsplit(self.path).path.startswith(('/assets/', '/static/', '/play/')):
                    self.path = '/index.html'
            super().do_GET()

    print(f'Frontend development only: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(root))).serve_forever()


if __name__ == '__main__':
    main()
