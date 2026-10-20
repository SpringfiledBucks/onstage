"""Export the actual request contract without opening documentation endpoints."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Import constructs the module-level ASGI object. This explicit, unreachable PG
# URL is only for offline contract generation; neither object is initialized.
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://contract@127.0.0.1:1/contract')
from app.main import create_app

if __name__ == '__main__':
    app = create_app(database_url='postgresql+psycopg://contract@127.0.0.1:1/contract', public_origin='http://localhost', version=(ROOT / 'VERSION').read_text().strip())
    target = ROOT / 'docs/openapi.json'
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('Exported actual API request schema: docs/openapi.json')
