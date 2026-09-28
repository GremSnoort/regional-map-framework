#!/usr/bin/env python3
"""Credential storage and signed sessions for the regional map server."""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_AUTH_FILE = ROOT / ".runtime" / "users.json"
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60
USERNAME_RE = re.compile(r"[a-z][a-z0-9_.-]{2,63}")


def auth_file() -> Path:
    value = os.environ.get("RMF_AUTH_FILE")
    return Path(value).expanduser().absolute() if value else DEFAULT_AUTH_FILE


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def atomic_store(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def new_store() -> dict:
    return {"schema_version": 1, "session_secret": encode(secrets.token_bytes(32)), "users": {}}


def load_store(path: Path | None = None, create: bool = False) -> dict:
    path = path or auth_file()
    if path.is_symlink():
        raise ValueError(f"Credential store must not be a symlink: {path}")
    if not path.is_file():
        if not create:
            raise FileNotFoundError(f"Credential store is missing: {path}. Run: python3 manage.py auth-set-user <username>")
        value = new_store(); atomic_store(path, value); return value
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError(f"Credential store must not be accessible by group/others: chmod 600 {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("session_secret"), str) or not isinstance(value.get("users"), dict):
        raise ValueError(f"Invalid credential store: {path}")
    return value


def validate_username(username: str) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3-64 lowercase ASCII characters: letters, digits, '.', '_' or '-'")
    return username


def validate_password(password: str) -> None:
    size = len(password.encode("utf-8"))
    if size < 12:
        raise ValueError("Password must contain at least 12 UTF-8 bytes")
    if size > 1024:
        raise ValueError("Password is too long")


def password_digest(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_user(username: str, password: str, path: Path | None = None) -> None:
    username = validate_username(username); validate_password(password); path = path or auth_file()
    store = load_store(path, create=True); salt = secrets.token_bytes(16)
    store["users"][username] = {
        "salt": encode(salt),
        "password_hash": encode(password_digest(password, salt)),
        "iterations": ITERATIONS,
        "session_version": encode(secrets.token_bytes(16)),
        "changed_at": int(time.time()),
    }
    atomic_store(path, store)


def set_user_prompt(username: str, path: Path | None = None) -> None:
    first = getpass.getpass(f"New password for {username}: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise ValueError("Passwords do not match")
    set_user(username, first, path)
    print(f"User {username} created or updated; existing sessions were revoked")


def delete_user(username: str, path: Path | None = None) -> None:
    username = validate_username(username); path = path or auth_file(); store = load_store(path)
    if username not in store["users"]:
        raise ValueError(f"Unknown user: {username}")
    del store["users"][username]; atomic_store(path, store); print(f"User {username} deleted")


def list_users(path: Path | None = None) -> list[str]:
    return sorted(load_store(path)["users"])


def verify_password(username: str, password: str, path: Path | None = None) -> bool:
    path = path or auth_file(); store = load_store(path); record = store["users"].get(username)
    if record:
        try:
            salt = decode(record["salt"]); expected = decode(record["password_hash"]); iterations = int(record["iterations"])
        except (KeyError, TypeError, ValueError):
            return False
        if not 100_000 <= iterations <= 2_000_000:
            return False
    else:
        salt = b"\0" * 16; expected = b"\0" * 32; iterations = ITERATIONS
    actual = password_digest(password, salt, iterations)
    return bool(record) and hmac.compare_digest(actual, expected)


def session_seconds() -> int:
    try:
        return max(300, min(7 * 24 * 60 * 60, int(os.environ.get("RMF_SESSION_SECONDS", SESSION_SECONDS))))
    except ValueError:
        return SESSION_SECONDS


def create_session(username: str, path: Path | None = None, now: int | None = None) -> str:
    path = path or auth_file(); store = load_store(path); record = store["users"].get(username)
    if not record:
        raise ValueError("Unknown user")
    issued = int(time.time() if now is None else now)
    body = encode(json.dumps({"u": username, "iat": issued, "exp": issued + session_seconds(), "v": record["session_version"]}, separators=(",", ":")).encode())
    signature = encode(hmac.new(decode(store["session_secret"]), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_session(token: str, path: Path | None = None, now: int | None = None) -> str | None:
    try:
        body, supplied = token.split(".", 1); store = load_store(path or auth_file())
        expected = encode(hmac.new(decode(store["session_secret"]), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected): return None
        data = json.loads(decode(body)); username = data["u"]; record = store["users"].get(username)
        moment = int(time.time() if now is None else now)
        if not record or data.get("v") != record.get("session_version") or not int(data["iat"]) <= moment < int(data["exp"]): return None
        return username
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return None
