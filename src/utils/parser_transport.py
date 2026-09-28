"""解析尝试的请求上下文；代理、时间预算与会话均不跨请求共享。"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from urllib3.util import Timeout


class ParseFailure(requests.RequestException):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ProxyAccessFailure(requests.RequestException):
    """当前 IP 的访问失败；解析器即使捕获异常也不能继续使用该 IP。"""


@dataclass
class Attempt:
    deadline: float
    manager: object = None
    proxy: object = None
    failure: object = None
    cookie_required: bool = False
    sessions: list = field(default_factory=list)

    def check(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.failure = ParseFailure(504, "PARSE_TIMEOUT", "解析超时，请稍后重试")
        if self.failure:
            raise self.failure
        if self.proxy and not self.manager.valid(self.proxy):
            self.fail_proxy("代理已失效或过期")
        return remaining

    def fail_proxy(self, reason):
        self.failure = ProxyAccessFailure(reason)
        self.manager.invalidate(self.proxy, reason)
        raise self.failure


_current_attempt = ContextVar("parser_attempt", default=None)


def current_attempt():
    return _current_attempt.get()


def report_cookie_required():
    """记录平台登录校验；直连可继续原有兜底，代理访问则立即换 IP。"""
    attempt = current_attempt()
    if attempt:
        attempt.cookie_required = True
        if attempt.proxy:
            attempt.fail_proxy('平台要求 Cookie 或登录校验')


@contextmanager
def attempt_context(attempt):
    token = _current_attempt.set(attempt)
    try:
        yield attempt
    finally:
        for session in attempt.sessions:
            session.close()
        _current_attempt.reset(token)


class ParserSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.attempt = current_attempt()
        self.isolate_cookies = False
        if self.attempt:
            # 直连与代理路由均由本次解析控制，不读取进程级代理环境变量。
            self.trust_env = False
            self.attempt.sessions.append(self)

    def request(self, method, url, **kwargs):
        # 清理顶层调用间的 Cookie，但同一调用的重定向仍使用 Requests 原生 Cookie 行为。
        if self.isolate_cookies:
            self.cookies.clear()
        try:
            return super().request(method, url, **kwargs)
        finally:
            if self.isolate_cookies:
                self.cookies.clear()

    def send(self, request, **kwargs):
        attempt = self.attempt
        if not attempt:
            return super().send(request, **kwargs)
        remaining = attempt.check()
        if attempt.proxy:
            remaining = min(remaining, attempt.proxy.expires_at - time.time())
            if remaining <= 0:
                attempt.fail_proxy("代理已过期")
            kwargs["proxies"] = {"http": attempt.proxy.url, "https": attempt.proxy.url}
        else:
            kwargs["proxies"] = {}
        timeout = kwargs.get("timeout")
        if isinstance(timeout, Timeout):
            connect, read = timeout.connect_timeout, timeout.read_timeout
        elif isinstance(timeout, tuple):
            connect, read = timeout
        else:
            connect = read = timeout
        kwargs["timeout"] = Timeout(
            total=remaining,
            connect=min(connect or remaining, remaining),
            read=min(read or remaining, remaining),
        )
        try:
            response = super().send(request, **kwargs)
        except requests.RequestException as exc:
            if attempt.deadline <= time.monotonic():
                attempt.check()
            if attempt.proxy:
                attempt.fail_proxy(f"网络连接或代理请求失败：{type(exc).__name__}")
            raise
        attempt.check()
        target = response.headers.get('Location') or response.url
        path = urlparse(target).path.lower()
        if any(part in path.split('/') for part in ('login', 'signin', 'captcha', 'verify')):
            report_cookie_required()
        if attempt.proxy and (response.status_code in (401, 403, 407, 429) or response.status_code >= 500):
            response.close()
            attempt.fail_proxy(f"平台访问失败，HTTP {response.status_code}")
        return response
