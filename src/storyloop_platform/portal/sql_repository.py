"""Dialect-neutral player accounts, sessions, saves, and turn receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from storyloop_platform.portal.models import SaveRecord

SESSION_TTL_SECONDS = 7 * 24 * 3600


class SQLPlayerRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _username(value: str) -> str:
        name = value.strip().casefold() if isinstance(value, str) else ""
        if not re.fullmatch(r"[\w.-]{2,32}", name, flags=re.UNICODE):
            raise ValueError("username must contain 2–32 letters, digits, dots, dashes, or underscores")
        return name

    @staticmethod
    def _password(value: str) -> str:
        if not isinstance(value, str) or len(value) < 8 or len(value) > 256:
            raise ValueError("password must contain 8–256 characters")
        return value

    @staticmethod
    def _save(row) -> SaveRecord:
        return SaveRecord(row["game_id"], row["player_id"], row["catalog_id"],
                          row["package_id"], row["package_version"], row["package_hash"],
                          row["created_at"], row["play_mode"])

    def issue_signup_invite(self, valid_days: int = 30) -> str:
        if type(valid_days) is not int or valid_days < 1:
            raise ValueError("invite validity must be at least one day")
        code = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO signup_invites (code_hash,expires_at,created_at)
                VALUES (:code_hash,:expires_at,:created_at)"""),
                {"code_hash": hashlib.sha256(code.encode()).digest(),
                 "expires_at": now + valid_days * 24 * 3600, "created_at": now})
        return code

    def register(self, username: str, password: str,
                 invite_code: str | None = None, *, require_invite: bool = False) -> str:
        name = self._username(username)
        secret = self._password(password)
        if require_invite and (not isinstance(invite_code, str) or not invite_code.strip()):
            raise ValueError("invite code required")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 310_000)
        player_id = uuid4().hex
        try:
            with self.engine.begin() as db:
                if require_invite:
                    now = int(time.time())
                    consumed = db.execute(text("""UPDATE signup_invites
                        SET used_by=:player_id, used_at=:used_at
                        WHERE code_hash=:code_hash AND used_by IS NULL
                        AND revoked_at IS NULL AND expires_at>:used_at"""),
                        {"player_id": player_id, "used_at": now,
                         "code_hash": hashlib.sha256(invite_code.strip().encode()).digest()})
                    if consumed.rowcount != 1:
                        raise ValueError("invalid or used invite code")
                db.execute(text("""INSERT INTO player_accounts
                    (player_id,username,password_salt,password_hash,created_at)
                    VALUES (:player_id,:username,:salt,:digest,:created_at)"""),
                    {"player_id": player_id, "username": name, "salt": salt,
                     "digest": digest, "created_at": int(time.time())})
        except IntegrityError as error:
            raise ValueError("username already exists") from error
        return player_id

    def authenticate(self, username: str, password: str) -> str:
        name = self._username(username)
        secret = self._password(password)
        with self.engine.connect() as db:
            row = db.execute(text("SELECT * FROM player_accounts WHERE username=:username"),
                             {"username": name}).mappings().first()
        if row is None:
            raise PermissionError("invalid username or password")
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), row["password_salt"], 310_000)
        if not hmac.compare_digest(digest, row["password_hash"]):
            raise PermissionError("invalid username or password")
        if row["status"] != "active":
            raise PermissionError("account suspended")
        return row["player_id"]

    def issue_token(self, player_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO player_sessions (token_hash,player_id,expires_at)
                VALUES (:token_hash,:player_id,:expires_at)"""),
                {"token_hash": hashlib.sha256(token.encode()).digest(),
                 "player_id": player_id, "expires_at": int(time.time()) + SESSION_TTL_SECONDS})
        return token

    def resolve_token(self, token: str) -> str:
        if not isinstance(token, str) or not token:
            raise PermissionError("login required")
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT s.player_id,s.expires_at,a.status
                FROM player_sessions s JOIN player_accounts a ON a.player_id=s.player_id
                WHERE s.token_hash=:token_hash"""),
                {"token_hash": hashlib.sha256(token.encode()).digest()}).mappings().first()
        if row is None or row["expires_at"] <= int(time.time()) or row["status"] != "active":
            raise PermissionError("login required")
        return row["player_id"]

    def revoke_token(self, token: str) -> None:
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM player_sessions WHERE token_hash=:token_hash"),
                       {"token_hash": hashlib.sha256(token.encode()).digest()})

    def create_save(self, player_id: str, catalog_id: str, game_id: str,
                    package_id: str, package_version: str, package_hash: str,
                    play_mode: str | None = None) -> SaveRecord:
        record = SaveRecord(game_id, player_id, catalog_id, package_id, package_version,
                            package_hash, int(time.time()), play_mode)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO player_saves
                (game_id,player_id,catalog_id,package_id,package_version,package_hash,created_at,play_mode)
                VALUES (:game_id,:player_id,:catalog_id,:package_id,:package_version,:package_hash,:created_at,:play_mode)"""),
                record.__dict__)
        return record

    def get_save(self, player_id: str, game_id: str) -> SaveRecord:
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT * FROM player_saves
                WHERE player_id=:player_id AND game_id=:game_id"""),
                {"player_id": player_id, "game_id": game_id}).mappings().first()
        if row is None:
            raise KeyError("save not found")
        return self._save(row)

    def list_saves(self, player_id: str) -> tuple[SaveRecord, ...]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT * FROM player_saves WHERE player_id=:player_id
                ORDER BY created_at DESC,game_id"""),
                {"player_id": player_id}).mappings().all()
        return tuple(self._save(row) for row in rows)

    def get_turn_response(self, game_id: str, request_id: str, text_input: str) -> dict | None:
        return self.get_turn_response_by_hash(game_id, request_id,
                hashlib.sha256(text_input.encode()).hexdigest())

    def get_turn_response_by_hash(self, game_id: str, request_id: str,
                                  input_hash: str) -> dict | None:
        if type(input_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", input_hash):
            raise ValueError("invalid input hash")
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT input_hash,response_json FROM portal_turns
                WHERE game_id=:game_id AND request_id=:request_id"""),
                {"game_id": game_id, "request_id": request_id}).mappings().first()
        if row is None:
            return None
        if not hmac.compare_digest(row["input_hash"], bytes.fromhex(input_hash)):
            raise ValueError("request ID already belongs to different input")
        return json.loads(row["response_json"])

    def latest_turn_response(self, game_id: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT response_json FROM portal_turns
                WHERE game_id=:game_id ORDER BY created_order DESC,request_id DESC LIMIT 1"""),
                {"game_id": game_id}).mappings().first()
        return json.loads(row["response_json"]) if row else None

    def store_intro(self, game_id: str, response: dict) -> None:
        payload = json.dumps(response, ensure_ascii=False)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO portal_intros (game_id,response_json)
                VALUES (:game_id,:response_json)
                ON CONFLICT (game_id) DO NOTHING"""),
                {"game_id": game_id, "response_json": payload})

    def get_intro(self, game_id: str) -> dict | None:
        with self.engine.connect() as db:
            row = db.execute(text("SELECT response_json FROM portal_intros WHERE game_id=:game_id"),
                             {"game_id": game_id}).mappings().first()
        return json.loads(row["response_json"]) if row else None

    def list_turns(self, game_id: str) -> list[dict]:
        with self.engine.connect() as db:
            rows = db.execute(text("""SELECT request_id,input_text,response_json FROM portal_turns
                WHERE game_id=:game_id ORDER BY created_order,request_id"""),
                {"game_id": game_id}).mappings().all()
        return [{"request_id": row["request_id"], "input": row["input_text"],
                 "response": json.loads(row["response_json"])} for row in rows]

    def store_turn_response(self, game_id: str, request_id: str, text_input: str,
                            response: dict) -> None:
        try:
            with self.engine.begin() as db:
                db.execute(text("""INSERT INTO portal_turns
                    (game_id,request_id,input_hash,input_text,response_json,created_order)
                    VALUES (:game_id,:request_id,:input_hash,:input_text,:response_json,:created_order)"""),
                    {"game_id": game_id, "request_id": request_id,
                     "input_hash": hashlib.sha256(text_input.encode()).digest(),
                     "input_text": text_input,
                     "response_json": json.dumps(response, ensure_ascii=False),
                     "created_order": time.time_ns()})
        except IntegrityError as error:
            cached = self.get_turn_response(game_id, request_id, text_input)
            if cached != response:
                raise ValueError("request ID already has a different response") from error
