"""Loopback workbench authentication with authoritative Redis sessions."""
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
from threading import BoundedSemaphore
import time

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from repo_agent.backend.schemas.task_contract import TaskError


SESSION_SECONDS = 8 * 60 * 60
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
LOGIN = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')
FAILURE_LIMIT = 5
FAILURE_SECONDS = 5 * 60

# Reserve an attempt before verifying a password, including concurrent attempts.
# The first increment and its expiry execute atomically.
RESERVE_ATTEMPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 1
"""


def normalize_login(login):
    if isinstance(login, str):
        login = login.strip().lower()
        if LOGIN.fullmatch(login):
            return login
    raise TaskError(400, 'INVALID_LOGIN', '登录名须为 1 至 64 位字母、数字、点、下划线或连字符')


def valid_password(password):
    if not isinstance(password, str):
        return False
    try:
        return 0 < len(password.encode('utf-8')) <= 1024
    except UnicodeEncodeError:
        return False


def session_required():
    return TaskError(401, 'SESSION_REQUIRED', '请登录工作台')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Identity:
    account_id: str
    login: str
    admin: bool
    csrf: str
    expires_at: int

    def response(self):
        return {'authRequired': True, 'account': {'id': self.account_id, 'login': self.login,
                'admin': self.admin}, 'csrfToken': self.csrf}


class AuthService:
    def __init__(self, accounts, redis_url, database_url):
        self.accounts = accounts
        self.client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.5,
                                           socket_timeout=0.5, retry=Retry(NoBackoff(), 0))
        self.namespace = 'coding-agent:auth:v1:' + digest(database_url)
        self.hasher = PasswordHasher(type=Type.ID)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        self.password_slots = BoundedSemaphore(2)

    def close(self):
        self.client.close()

    def _call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except redis.RedisError as exc:
            raise TaskError(503, 'SESSION_STORE_UNAVAILABLE', '会话服务暂不可用，请稍后重试') from exc

    def _session_key(self, token):
        return self.namespace + ':session:' + digest(token)

    def login(self, login, password):
        invalid = TaskError(401, 'INVALID_CREDENTIALS', '登录名或密码错误')
        try:
            login = normalize_login(login)
        except TaskError:
            raise invalid from None
        if not valid_password(password):
            raise invalid
        attempts = self.namespace + ':attempts:' + digest(login)
        if not self.password_slots.acquire(blocking=False):
            raise TaskError(503, 'LOGIN_BUSY', '登录服务繁忙，请稍后重试')
        try:
            if not self._call(self.client.eval, RESERVE_ATTEMPT, 1, attempts, FAILURE_LIMIT, FAILURE_SECONDS):
                raise TaskError(429, 'LOGIN_RATE_LIMITED', '登录尝试过多，请稍后重试')
            account = self.accounts.by_login(login)
            if not self.hasher.verify(account.password_hash if account else self.dummy_hash, password):
                raise invalid
        except VerifyMismatchError:
            raise invalid from None
        finally:
            self.password_slots.release()
        if not account or not account.enabled:
            raise invalid
        csrf = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_SECONDS
        session = {'account_id': account.account_id, 'version': account.session_version,
                   'csrf': csrf, 'expires_at': expires_at}
        self._call(self.client.delete, attempts)
        if not self._call(self.client.set, self._session_key(token), json.dumps(session),
                          ex=SESSION_SECONDS, nx=True):
            raise TaskError(503, 'SESSION_CREATE_FAILED', '暂时无法创建会话，请重试')
        return token, Identity(account.account_id, account.login, account.admin, csrf, expires_at)

    def authenticate(self, token):
        if not TOKEN.fullmatch(token or ''):
            raise session_required()
        raw = self._call(self.client.get, self._session_key(token))
        if raw is None:
            raise session_required()
        try:
            session = json.loads(raw)
            account_id, version = session['account_id'], session['version']
            csrf, expiry = session['csrf'], session['expires_at']
            if not (isinstance(account_id, str) and re.fullmatch(r'[0-9a-f]{32}', account_id)
                    and type(version) is int and type(expiry) is int
                    and isinstance(csrf, str) and TOKEN.fullmatch(csrf)):
                raise ValueError('Invalid session fields')
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise TaskError(503, 'INVALID_SESSION_RECORD', '会话状态暂不可用') from exc
        if expiry <= time.time():
            raise session_required()
        account = self.accounts.active_identity(account_id, version, digest(token))
        if account is None:
            raise session_required()
        login, admin = account
        return Identity(account_id, login, admin, csrf, expiry)

    def logout(self, token, identity):
        self._call(self.client.delete, self._session_key(token))
        # Report success after PostgreSQL commits, so restoring an older Redis
        # snapshot cannot bring back a session whose logout was acknowledged.
        self.accounts.revoke_session(digest(token), identity.expires_at, int(time.time()))
