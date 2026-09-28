"""2026-09-28 Codex：少量快手直连及代理真实核验，使用临时数据库。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from app import create_app

api_url = dotenv_values('.env')['JULIANG_PROXY_API_URL']
url = 'https://v.m.chenzhongtech.com/fw/photo/3xbr5pi8hxi4e6s'
with tempfile.TemporaryDirectory() as temp:
    app = create_app({'TESTING': True, 'API_ONLY': True, 'SECRET_KEY': 'live-validation',
                      'DATABASE': str(Path(temp) / 'live.db'), 'JULIANG_PROXY_API_URL': ''})
    manager = app.extensions['proxy_manager']
    client = app.test_client()
    for mode in ('direct', 'proxy'):
        if mode == 'proxy':
            manager.api_url = api_url
            manager.activate('快手')
        start = time.monotonic()
        result = client.get('/api/v1/parse', query_string={'url': url})
        data = result.get_json()
        print(json.dumps({'mode': mode, 'status': result.status_code,
                          'error_code': data.get('error_code'),
                          'has_media': bool((data.get('data') or {}).get('video_url') or (data.get('data') or {}).get('image_list')),
                          'elapsed_seconds': round(time.monotonic() - start, 2)}, ensure_ascii=False), flush=True)
    with manager.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT expires_at, invalid, retired FROM proxy_addresses')]
    print(json.dumps({'proxy_inventory': rows}, ensure_ascii=False), flush=True)
