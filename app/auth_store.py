"""本地账号、密码校验与浏览器会话存储。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 600_000


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    role: str
    active: bool
    created_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at,
        }


class AuthError(ValueError):
    """账号操作违反业务约束。"""


def _project_root() -> Path:
    """app/auth_store.py -> app -> 项目根。"""
    return Path(__file__).resolve().parent.parent


def _database_path() -> Path:
    configured = os.environ.get("VERIMIND_AUTH_DB_PATH")
    if configured:
        # 相对路径按**项目根**解析，不按进程 CWD：.env 里写的是
        # ./agent_temp/verimind-accounts.sqlite3，若从别的目录启动 uvicorn，
        # 按 CWD 解析会静默在别处新建一个空库 —— 表现就是"账号又登不上了"。
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = _project_root() / candidate
        return candidate.resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "VeriMind" / "app" / "accounts.sqlite3"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "verimind" / "accounts.sqlite3"
    return Path.home() / ".local" / "share" / "verimind" / "accounts.sqlite3"


def _password_digest(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, PASSWORD_ITERATIONS
    )
    return actual_salt, digest


class AuthStore:
    """独立 SQLite 应用库；业务查询仍只访问原有业务数据库。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
                """
            )
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    @staticmethod
    def _user(row: sqlite3.Row) -> AuthUser:
        return AuthUser(
            id=str(row["id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
        )

    def create_user(self, username: str, password: str, role: str = "user") -> AuthUser:
        clean_name = username.strip()
        if not 3 <= len(clean_name) <= 48 or any(ch.isspace() for ch in clean_name):
            raise AuthError("用户名须为 3 至 48 个不含空格的字符")
        if len(password) < 12 or len(password) > 256:
            raise AuthError("密码长度须为 12 至 256 个字符")
        if role not in {"admin", "user"}:
            raise AuthError("账号角色无效")
        salt, digest = _password_digest(password)
        now_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        user_id = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users(id, username, password_salt, password_hash, role, active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (user_id, clean_name, salt, digest, role, now_text, now_text),
                )
                row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise AuthError("用户名已存在") from exc
        if row is None:
            raise AuthError("账号创建失败")
        return self._user(row)

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
        if row is None:
            # Keep unknown-user and wrong-password work factors equivalent.
            _password_digest(password, b"verimind-auth-dummy")
            return None
        _, candidate = _password_digest(password, bytes(row["password_salt"]))
        if not hmac.compare_digest(candidate, bytes(row["password_hash"])) or not row["active"]:
            return None
        return self._user(row)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, now, now + SESSION_TTL_SECONDS, now),
            )
        return token

    def resolve_session(self, token: str | None) -> AuthUser | None:
        if not token or len(token) > 256:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1",
                (token_hash, now),
            ).fetchone()
        return self._user(row) if row is not None else None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def list_users(self) -> list[AuthUser]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY created_at, username").fetchall()
        return [self._user(row) for row in rows]

    def get_user_by_username(self, username: str) -> AuthUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
        return self._user(row) if row is not None else None

    def set_user(self, user_id: str, *, active: bool, role: str, actor_id: str) -> AuthUser:
        if role not in {"admin", "user"}:
            raise AuthError("账号角色无效")
        with self._connect() as connection:
            target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise AuthError("账号不存在")
            will_be_admin = active and role == "admin"
            if not will_be_admin and target["role"] == "admin" and target["active"]:
                active_admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE active = 1 AND role = 'admin'"
                ).fetchone()[0]
                if active_admin_count <= 1:
                    raise AuthError("必须至少保留一个启用的管理员")
            if user_id == actor_id and (not active or role != "admin"):
                raise AuthError("不能停用或降级当前管理员账号")
            now_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            connection.execute(
                "UPDATE users SET active = ?, role = ?, updated_at = ? WHERE id = ?",
                (int(active), role, now_text, user_id),
            )
            if not active:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row)

    def reset_password(self, user_id: str, password: str) -> None:
        if len(password) < 12 or len(password) > 256:
            raise AuthError("密码长度须为 12 至 256 个字符")
        salt, digest = _password_digest(password)
        now_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_salt = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                (salt, digest, now_text, user_id),
            )
            if cursor.rowcount == 0:
                raise AuthError("账号不存在")
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def change_password(self, user_id: str, current_password: str, new_password: str) -> str:
        if len(new_password) < 12 or len(new_password) > 256:
            raise AuthError("新密码长度须为 12 至 256 个字符")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or not row["active"]:
                raise AuthError("账号不存在或已停用")
            _, candidate = _password_digest(current_password, bytes(row["password_salt"]))
            if not hmac.compare_digest(candidate, bytes(row["password_hash"])):
                raise AuthError("当前密码不正确")
            salt, digest = _password_digest(new_password)
            now_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            connection.execute(
                "UPDATE users SET password_salt = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                (salt, digest, now_text, user_id),
            )
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return self.create_session(user_id)


__all__ = ["AuthError", "AuthStore", "AuthUser", "SESSION_TTL_SECONDS"]
