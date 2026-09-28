"""统一会话的真实 HTTP Cookie 隔离及代理传输回归。"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests

from src.parsers.kuaishou_parser import KuaishouParser
from src.utils.parser_transport import Attempt, ParserSession, ParseFailure, ProxyAccessFailure, attempt_context
from src.utils.proxy_manager import ProxyAddress
from utils.web_fetcher import WebFetcher


def response_for(request, text='ok', status=200, headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    response.url = request.url
    response.request = request
    response.headers.update(headers or {})
    return response


class TransportTest(unittest.TestCase):
    def test_cookie_isolation_preserves_redirect_and_explicit_cookie(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Cookie'), self.headers.get('Referer')))
                if self.path == '/start':
                    self.send_response(302)
                    self.send_header('Location', '/end')
                    self.send_header('Set-Cookie', 'temporary=one; Path=/')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with ParserSession() as session:
                    session.isolate_cookies = True
                    session.trust_env = False
                    base = f'http://127.0.0.1:{server.server_port}'
                    session.get(base + '/start', headers={'Referer': 'mobile'}, timeout=5)
                    self.assertEqual(len(session.cookies), 0)
                    session.get(base + '/next', headers={'Referer': 'desktop'}, timeout=5)
                    session.get(base + '/explicit', headers={'Cookie': 'configured=yes'}, timeout=5)
                self.assertEqual(seen[1][1], 'temporary=one')
                self.assertIsNone(seen[2][1])
                self.assertEqual(seen[2][2], 'desktop')
                self.assertEqual(seen[3][1], 'configured=yes')
                self.assertIsNone(seen[3][2])
            finally:
                server.shutdown()
                thread.join()

    def test_proxy_applies_to_shortlink_and_parser_constructor(self):
        calls = []
        manager = Mock()
        manager.valid.return_value = True
        proxy = ProxyAddress('127.0.0.1:8888', time.time() + 49)
        html = 'window.INIT_STATE=' + json.dumps({'photo': {'caption': '文案', 'mainMvUrls': [{'url': 'https://cdn.example/video.mp4'}]}})

        def send(adapter, request, **kwargs):
            calls.append((request, kwargs))
            if request.url.startswith('https://v.kuaishou.com'):
                return response_for(request, status=302, headers={'Location': 'https://v.m.chenzhongtech.com/fw/photo/123'})
            return response_for(request, html)

        with patch('requests.adapters.HTTPAdapter.send', send):
            with attempt_context(Attempt(time.monotonic() + 90, manager, proxy)):
                url = WebFetcher.fetch_redirect_url('https://v.kuaishou.com/123')
                parser = KuaishouParser(url)
                self.assertEqual(parser.get_real_video_url(), 'https://cdn.example/video.mp4')
        self.assertGreaterEqual(len(calls), 2)
        for request, kwargs in calls:
            self.assertEqual(kwargs['proxies'], {'http': proxy.url, 'https': proxy.url})
            self.assertLessEqual(kwargs['timeout'].connect_timeout, 5)
        self.assertTrue(parser.session.isolate_cookies)

    def test_direct_ignores_environment_proxy(self):
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://unwanted:8888'}):
            with patch('requests.adapters.HTTPAdapter.send', autospec=True) as send:
                send.side_effect = lambda adapter, request, **kwargs: response_for(request)
                with attempt_context(Attempt(time.monotonic() + 90)):
                    with ParserSession() as session:
                        session.get('https://www.kuaishou.com/test', timeout=5)
                self.assertEqual(send.call_args.kwargs['proxies'], {})

    def test_failure_is_sticky_and_deadline_is_not_swallowed(self):
        manager = Mock()
        manager.valid.return_value = True
        proxy = ProxyAddress('127.0.0.1:8888', time.time() + 49)
        with patch('requests.adapters.HTTPAdapter.send', side_effect=requests.Timeout()) as send:
            with attempt_context(Attempt(time.monotonic() + 90, manager, proxy)):
                session = ParserSession()
                for _ in range(2):
                    with self.assertRaises(ProxyAccessFailure):
                        session.get('https://www.kuaishou.com/test', timeout=5)
            self.assertEqual(send.call_count, 1)
        manager.invalidate.assert_called_once()
        with attempt_context(Attempt(time.monotonic() - 1)):
            with self.assertRaises(ParseFailure) as caught:
                ParserSession().get('https://www.kuaishou.com/test', timeout=5)
        self.assertEqual(caught.exception.code, 'PARSE_TIMEOUT')

    def test_http_failures_discard_but_404_is_left_to_parser(self):
        for status in (401, 403, 407, 429, 500, 502, 404):
            with self.subTest(status=status):
                manager = Mock()
                manager.valid.return_value = True
                proxy = ProxyAddress('127.0.0.1:8888', time.time() + 49)
                with patch('requests.adapters.HTTPAdapter.send', autospec=True) as send:
                    send.side_effect = lambda adapter, request, **kwargs: response_for(request, status=status)
                    with attempt_context(Attempt(time.monotonic() + 90, manager, proxy)):
                        if status == 404:
                            self.assertEqual(ParserSession().get('https://www.kuaishou.com/test').status_code, 404)
                            manager.invalidate.assert_not_called()
                        else:
                            with self.assertRaises(ProxyAccessFailure):
                                ParserSession().get('https://www.kuaishou.com/test')
                            manager.invalidate.assert_called_once()

    def test_budget_shortens_existing_timeout(self):
        with patch('requests.adapters.HTTPAdapter.send', autospec=True) as send:
            send.side_effect = lambda adapter, request, **kwargs: response_for(request)
            with attempt_context(Attempt(time.monotonic() + 1)):
                ParserSession().get('https://www.kuaishou.com/test', timeout=5)
            self.assertLessEqual(send.call_args.kwargs['timeout'].connect_timeout, 1)
