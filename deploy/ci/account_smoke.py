"""Synthetic default-account-mode runtime smoke; loopback only, no saved secrets."""
import argparse
import secrets
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    origin = args.url.rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.path or parsed.username or parsed.password:
        parser.error('Only disposable loopback runtimes are accepted')
    clients = []
    try:
        for _ in range(2):
            client = httpx.Client(base_url=origin, headers={'Origin': origin}, trust_env=False, timeout=10)
            clients.append(client)
            bootstrap = client.post('/api/session', json={})
            bootstrap.raise_for_status()
            client.headers['X-CSRF-Token'] = bootstrap.json()['csrf_token']
            auth = client.post('/api/auth/register', json={'username': 'ci' + secrets.token_hex(8), 'password': secrets.token_urlsafe(24)})
            auth.raise_for_status()
            client.headers['X-CSRF-Token'] = auth.json()['csrf_token']
        host, actor = clients
        def post(client, path, body):
            result = client.post(path, json=body)
            result.raise_for_status()
            return result.json()
        rid = post(host, '/api/rooms', {'scenario_id': 'mygo_rehearsal'})['room']['id']
        path = '/api/rooms/' + rid
        post(actor, path + '/join', {})
        post(host, path + '/characters/claim', {'character_id': 'mygo.tomori'})
        post(actor, path + '/characters/claim', {'character_id': 'mygo.anon'})
        post(host, path + '/control', {'action': 'start'})
        cookie = '; '.join(k + '=' + v for k, v in actor.cookies.items())
        with connect(origin.replace('http://', 'ws://') + path + '/stream', origin=origin,
                     additional_headers={'Cookie': cookie}, proxy=None, open_timeout=5, close_timeout=2) as websocket:
            websocket.send('ping')
            websocket.recv(timeout=5)
            body = {'channel': 'stage', 'text': 'Synthetic account image smoke', 'client_message_id': secrets.token_hex(16)}
            message = post(actor, path + '/messages', body)['message']
            assert post(actor, path + '/messages', body)['message']['id'] == message['id']
        post(actor, path + '/share-permission', {'allowed': True})
        share = post(host, path + '/shares', {'message_ids': [message['id']]})
        assert httpx.get(origin + '/api/shares/' + share['slug'], trust_env=False).status_code == 200
        post(host, path + '/control', {'action': 'end'})
        post(actor, path + '/rating', {'rating': 5})
        print('VERIFIED: default account HTTP/WS, idempotency, consented share and rating; synthetic loopback only')
    finally:
        for client in clients:
            client.close()


if __name__ == '__main__':
    main()
