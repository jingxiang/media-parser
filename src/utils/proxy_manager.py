"""巨量代理库存与平台窗口；通过 SQLite 短事务协调同容器内的工作进程。"""

import ipaddress
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import requests
from urllib3.util import Timeout

from configs.logging_config import get_logger

logger = get_logger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_platform_windows (
    platform TEXT PRIMARY KEY, until_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proxy_addresses (
    address TEXT PRIMARY KEY, expires_at REAL NOT NULL,
    created_at REAL NOT NULL, invalid INTEGER NOT NULL DEFAULT 0,
    retired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proxy_coordination (
    id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0, next_fetch REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO proxy_coordination(id) VALUES(1);
CREATE TABLE IF NOT EXISTS proxy_active_uses (
    token TEXT PRIMARY KEY, until_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class ProxyAddress:
    address: str
    expires_at: float

    @property
    def url(self):
        return f"http://{self.address}"


class ProxyManager:
    def __init__(self, database, api_url):
        self.database = database
        self.api_url = api_url
        self._thread = None
        self._pid = None
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        with self.connection() as db:
            db.executescript(SCHEMA)

    @property
    def enabled(self):
        return bool(self.api_url)

    @contextmanager
    def connection(self, write=False):
        # 独立连接，避免与请求积分事务或 Flask 的线程局部连接互相干扰。
        db = sqlite3.connect(self.database, timeout=1, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def active(self, platform):
        if not self.enabled or not platform:
            return False
        with self.connection() as db:
            row = db.execute(
                "SELECT until_at FROM proxy_platform_windows WHERE platform=?", (platform,)
            ).fetchone()
        return bool(row and row["until_at"] > time.time())

    def activate(self, platform):
        now = time.time()
        with self.connection(write=True) as db:
            changed = db.execute(
                "INSERT INTO proxy_platform_windows(platform, until_at) VALUES(?, ?) "
                "ON CONFLICT(platform) DO UPDATE SET until_at=excluded.until_at "
                "WHERE proxy_platform_windows.until_at<=?",
                (platform, now + 900, now),
            ).rowcount
        if changed:
            logger.info("平台切换代理访问：%s，持续 900 秒", platform)
        self._wake.set()

    def begin_use(self, remaining):
        token = uuid.uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO proxy_active_uses VALUES(?, ?)", (token, time.time() + remaining))
        return token

    def end_use(self, token):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM proxy_active_uses WHERE token=?", (token,))

    def available(self, excluded=()):
        now = time.time()
        with self.connection() as db:
            rows = db.execute(
                "SELECT address, expires_at FROM proxy_addresses "
                "WHERE invalid=0 AND retired=0 AND expires_at>? ORDER BY created_at, address",
                (now + 10,),
            ).fetchall()
        return next((ProxyAddress(row["address"], row["expires_at"])
                     for row in rows if row["address"] not in excluded), None)

    def valid(self, proxy):
        with self.connection() as db:
            row = db.execute(
                "SELECT invalid, expires_at FROM proxy_addresses WHERE address=?", (proxy.address,)
            ).fetchone()
        # 退休仅停止分配，进行中的尝试仍可使用到有效期结束。
        return bool(row and not row["invalid"] and row["expires_at"] > time.time())

    def invalidate(self, proxy, reason):
        with self.connection(write=True) as db:
            changed = db.execute(
                "UPDATE proxy_addresses SET invalid=1 WHERE address=? AND invalid=0", (proxy.address,)
            ).rowcount
        if changed:
            logger.info("剔除代理 %s：%s", proxy.address, reason)
        self._wake.set()

    @staticmethod
    def decode(payload, started):
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ValueError("代理供应商返回失败")
        rows = payload.get("data", {}).get("proxy_list")
        if not isinstance(rows, list) or not rows:
            raise ValueError("代理列表为空")
        row = rows[0]
        ip = str(ipaddress.IPv4Address(row["ip"]))
        port = int(row["port"])
        remain = float(row["ip_remain"])
        if not 1 <= port <= 65535 or not 10 < remain < float("inf"):
            raise ValueError("代理端口或有效期无效")
        expires = started + remain
        if expires <= time.time() + 10:
            raise ValueError("代理剩余有效期不足")
        return ProxyAddress(f"{ip}:{port}", expires)

    def replenish(self, deadline):
        """领取一次提取租约；返回是否实际调用供应商，便于请求计数。"""
        if not self.enabled or deadline - time.monotonic() <= 0:
            return False
        now = time.time()
        owner = uuid.uuid4().hex
        with self.connection(write=True) as db:
            db.execute("DELETE FROM proxy_addresses WHERE expires_at<=?", (now,))
            db.execute("UPDATE proxy_addresses SET retired=1 WHERE expires_at<=?", (now + 20,))
            count = db.execute(
                "SELECT count(*) FROM proxy_addresses WHERE invalid=0 AND retired=0"
            ).fetchone()[0]
            row = db.execute("SELECT * FROM proxy_coordination WHERE id=1").fetchone()
            if count >= 2 or row["lease_until"] > now or row["next_fetch"] > now:
                return False
            db.execute(
                "UPDATE proxy_coordination SET owner=?, lease_until=?, next_fetch=? WHERE id=1",
                (owner, now + 8, now + 1),
            )
        proxy = None
        try:
            # 不继承服务环境中的 HTTP_PROXY，供应商请求必须直连。
            with requests.Session() as session:
                session.trust_env = False
                budget = min(5, deadline - time.monotonic())
                if budget <= 0:
                    raise ValueError("解析预算已耗尽")
                response = session.get(
                    self.api_url, timeout=Timeout(total=budget, connect=min(2, budget), read=budget),
                )
                response.raise_for_status()
                proxy = self.decode(response.json(), now)
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
            # 不记录完整异常，避免将含签名的提取 URL 写入日志。
            logger.warning("代理提取失败：网络、供应商状态或响应字段异常")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM proxy_coordination WHERE id=1").fetchone()
            if row["owner"] != owner:
                return True
            accepted = False
            if proxy and proxy.expires_at > time.time() + 10:
                existing = db.execute(
                    "SELECT * FROM proxy_addresses WHERE address=?", (proxy.address,)
                ).fetchone()
                if not existing:
                    db.execute("INSERT INTO proxy_addresses(address, expires_at, created_at) VALUES(?, ?, ?)",
                               (proxy.address, proxy.expires_at, now))
                    accepted = True
                # 重复地址不延长旧 IP 的寿命，也不复活已剔除的地址。
            failures = 0 if accepted else row["failures"] + 1
            delay = 1 if accepted else (1, 2, 4, 8, 15)[min(failures - 1, 4)]
            db.execute(
                "UPDATE proxy_coordination SET owner=NULL, lease_until=0, next_fetch=?, failures=? WHERE id=1",
                (max(now + 1, time.time() + (0 if accepted else delay)), failures),
            )
        if accepted:
            logger.info("已补充代理 %s，剩余有效期 %.0f 秒", proxy.address, proxy.expires_at - time.time())
        return True

    def maintain_once(self):
        now = time.time()
        with self.connection(write=True) as db:
            expired = db.execute("SELECT platform FROM proxy_platform_windows WHERE until_at<=?", (now,)).fetchall()
            db.execute("DELETE FROM proxy_platform_windows WHERE until_at<=?", (now,))
            db.execute("DELETE FROM proxy_active_uses WHERE until_at<=?", (now,))
            needed = db.execute("SELECT 1 FROM proxy_platform_windows LIMIT 1").fetchone() or db.execute(
                "SELECT 1 FROM proxy_active_uses LIMIT 1"
            ).fetchone()
        for row in expired:
            logger.info("平台恢复直接访问：%s", row["platform"])
        if needed:
            self.replenish(time.monotonic() + 5)

    def start(self):
        if not self.enabled:
            return
        with self._start_lock:
            if self._pid == os.getpid() and self._thread and self._thread.is_alive():
                return
            self._pid = os.getpid()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._maintain, daemon=True, name="proxy-maintenance")
            self._thread.start()

    def _maintain(self):
        while not self._stop.is_set():
            try:
                self.maintain_once()
            except Exception:
                logger.exception("代理库存维护异常")
            self._wake.wait(1)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=6)
