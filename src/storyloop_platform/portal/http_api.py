"""FastAPI adapter for the independent player portal service."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from storyloop_harness.generation import (
    ExecutionRequestLimitExceeded, ExecutionTimeout,
    GenerationRequestLimitExceeded, GenerationTimeout,
)
from starlette.middleware.cors import CORSMiddleware

from storyloop_platform.api.contracts import History, TurnFailure, TurnRecoveryStatus, View, stream_event_adapter
from storyloop_platform.lifecycle import ServiceStopping, GRACE_TIMEOUT, CANCEL_TIMEOUT
from storyloop_platform.portal.service import PlayerPortal
from storyloop_platform.portal.access import AccessDenied
from storyloop_platform.portal.sql_repository import SESSION_TTL_SECONDS
from storyloop_platform.portal.user_scenarios import MAX_ARCHIVE_BYTES
from storyloop_platform.portal.turn_errors import TurnInputError, TurnRecoveryRequired, turn_failure


logger = logging.getLogger(__name__)
SSE_KEEPALIVE_SECONDS = 15
TRANSIENT_MODEL_ERRORS = (APIConnectionError, RateLimitError, InternalServerError)


def _model_failure_message(error: Exception) -> str:
    if isinstance(error, APITimeoutError):
        cause = "模型服务响应超时"
    elif isinstance(error, RateLimitError):
        cause = "模型服务暂时繁忙（请求限流）"
    elif isinstance(error, APIConnectionError):
        cause = "模型服务暂时无法连接"
    else:
        cause = "模型服务暂时不可用"
    return f"{cause}。请稍后重试这条行动；使用原请求重试不会重复扣费。"


def _model_http_error(error: Exception) -> HTTPException:
    status = 504 if isinstance(error, APITimeoutError) else 429 if isinstance(error, RateLimitError) else 503
    return HTTPException(status_code=status, detail=_model_failure_message(error))


class AccountBody(BaseModel):
    username: str
    password: str
    invite_code: str | None = Field(default=None, max_length=128)


class SaveBody(BaseModel):
    catalog_id: str
    play_mode: Literal["campaign", "freeform"] | None = None
    story_setup: dict[str, str] | None = None


class SaveSettingsBody(BaseModel):
    temperature: float
    context_window_tokens: int


class TurnBody(BaseModel):
    text: str
    request_id: str | None = None


class FactQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    fact_id: str = Field(min_length=1, max_length=256)
    speaker_id: str = Field(min_length=1, max_length=256)


class PlayerMemorySettingsBody(BaseModel):
    enabled: bool


class PlayerMemoryCorrectionBody(BaseModel):
    text: str


class ReviewDecisionBody(BaseModel):
    decision: str
    reason: str = ""


class RoleBody(BaseModel):
    role: str
    enabled: bool


class AccountStatusBody(BaseModel):
    status: str


class ReleaseStateBody(BaseModel):
    state: str
    reason: str = ""


class InvitationIssueBody(BaseModel):
    count: int = Field(default=1, ge=1, le=20)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, TurnRecoveryRequired):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, AccessDenied):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, PermissionError):
        return HTTPException(status_code=401, detail=str(error))
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=str(error.args[0]))
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))
    raise error


def create_app(portal: PlayerPortal, *, operation_shutdown_timeout: float = GRACE_TIMEOUT,
               operation_cancel_timeout: float = CANCEL_TIMEOUT) -> FastAPI:
    operations = portal.operations

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        operations.start(portal.memory_service.run_worker, name="memory-worker", service=True)
        try:
            yield
        finally:
            await portal.shutdown(grace_timeout=operation_shutdown_timeout,
                                  cancel_timeout=operation_cancel_timeout)

    app = FastAPI(title="Story Harness Player Portal", version="0.1.0", lifespan=lifespan)

    def failure_content(failure: dict) -> dict:
        return TurnFailure.model_validate(failure).model_dump(mode="json", exclude_unset=True)

    def invalid_contract_failure(request_id: str | None) -> dict:
        return failure_content(turn_failure(
            RuntimeError("invalid response"), request_id, code="INVALID_TURN_RESPONSE",
            message="未能确认这条行动的完整结果。请使用原请求重试；若持续失败，请联系管理员。"))

    def event_frame(event: dict) -> str:
        validated = stream_event_adapter.validate_python(event)
        payload = stream_event_adapter.dump_python(validated, mode="json", exclude_unset=True)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def stopping_failure(request_id: str | None) -> dict:
        message = "服务正在重启，这条行动尚未开始，请稍后再提交。"
        return failure_content({"code": "SERVICE_STOPPING", "message": message, "detail": message,
                "retryable": False, "commit_state": "not_started", "request_id": request_id})

    @app.exception_handler(ServiceStopping)
    async def service_stopping(_request: Request, _error: ServiceStopping) -> JSONResponse:
        return JSONResponse(status_code=503, content=stopping_failure(None))

    online_cookie = portal.settings.environment == "online"
    cookie_name = "__Host-storyloop" if online_cookie else "storyloop-session"

    async def model_error_response(_request: Request, error: Exception) -> JSONResponse:
        logger.warning("model unavailable during portal request: %s", type(error).__name__)
        mapped = _model_http_error(error)
        return JSONResponse({"detail": mapped.detail}, status_code=mapped.status_code)

    for error_type in TRANSIENT_MODEL_ERRORS:
        app.add_exception_handler(error_type, model_error_response)

    bearer = HTTPBearer(auto_error=False)
    if portal.settings.environment == "online":
        app.add_middleware(CORSMiddleware, allow_origins=list(portal.resources.allowed_origins()),
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def validate_request(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host not in portal.resources.allowed_hosts():
            return JSONResponse({"error": "invalid host"}, status_code=401)
        origin = request.headers.get("origin")
        if portal.settings.environment == "local":
            origins = {f"http://{host}:{request.url.port}", f"http://{host}",
                       "http://127.0.0.1:5173", "http://localhost:5173"}
        else:
            origins = set(portal.resources.allowed_origins())
        if origin and origin not in origins:
            return JSONResponse({"error": "cross-origin requests are not allowed"}, status_code=401)
        if (online_cookie and request.url.path.startswith("/v1/")
                and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not origin):
            return JSONResponse({"error": "Origin required"}, status_code=403)
        needs_json = (request.url.path in {"/v1/accounts", "/v1/sessions", "/v1/saves",
                                               "/v1/me/memory/settings", "/v1/manage/invites"}
                      or (request.method == "PATCH"
                          and request.url.path.startswith("/v1/me/memory/"))
                      or (request.url.path.startswith("/v1/manage/")
                          and request.url.path.endswith(("/decision", "/role", "/status", "/state")))
                      or request.url.path.endswith("/settings")
                      or request.url.path.endswith(("/turns", "/turns/stream", "/fact-queries")))
        if request.method in {"POST", "PUT", "PATCH"} and needs_json:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=400)
        if request.method == "POST" and request.url.path.endswith("/fact-queries"):
            max_bytes = 4096
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        return JSONResponse({"detail": "Fact query body exceeds 4 KiB"}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    return JSONResponse({"detail": "Fact query body exceeds 4 KiB"}, status_code=413)
            request._body = bytes(chunks)
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def token(request: Request,
              credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if online_cookie:
            if credentials is not None:
                raise HTTPException(status_code=401, detail="Cookie session required")
            session_token = request.cookies.get(cookie_name)
        else:
            session_token = credentials.credentials if credentials else request.cookies.get(cookie_name)
        if not session_token:
            raise HTTPException(status_code=401, detail="Login required")
        try:
            player_id = portal.accounts.resolve_token(session_token)
        except PermissionError as error:
            raise _http_error(error) from error
        request.state.user_id = player_id
        return session_token

    def set_session_cookie(response: Response, session_token: str) -> None:
        response.set_cookie(cookie_name, session_token, max_age=SESSION_TTL_SECONDS,
                            path="/", secure=online_cookie, httponly=True, samesite="lax")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/accounts", status_code=201)
    def register(body: AccountBody, response: Response) -> dict:
        try:
            session = portal.register(body.username, body.password, body.invite_code)
            set_session_cookie(response, session["token"])
            info = portal.session_info(session["token"])
            return info if online_cookie else {**session, **info}
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/sessions")
    def login(body: AccountBody, response: Response) -> dict:
        try:
            session = portal.login(body.username, body.password)
            set_session_cookie(response, session["token"])
            info = portal.session_info(session["token"])
            return info if online_cookie else {**session, **info}
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/sessions/current")
    def current_session(request: Request, auth: str = Depends(token)) -> dict:
        return portal.session_info(auth)

    @app.delete("/v1/sessions/current")
    def logout(response: Response, auth: str = Depends(token)) -> dict:
        portal.logout(auth)
        response.delete_cookie(cookie_name, path="/", secure=online_cookie,
                               httponly=True, samesite="lax")
        return {"status": "logged_out"}

    @app.get("/v1/catalog")
    def catalog(auth: str = Depends(token)) -> dict:
        return {"games": portal.games(auth)}

    @app.get("/v1/catalog/{catalog_id}/artwork/cover")
    def catalog_cover(catalog_id: str, auth: str = Depends(token)) -> FileResponse:
        try:
            return FileResponse(portal.cover_artwork(auth, catalog_id))
        except (KeyError, PermissionError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/cast")
    def visible_cast(game_id: str, auth: str = Depends(token)) -> dict:
        try:
            return {"cast": portal.cast(auth, game_id)}
        except (KeyError, PermissionError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/player-card")
    def player_card(game_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.player_card(auth, game_id)
        except (KeyError, PermissionError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/cast/{actor_id}")
    def character_detail(game_id: str, actor_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.character_detail(auth, game_id, actor_id)
        except (KeyError, PermissionError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/cast/{actor_id}/portrait")
    def cast_portrait(game_id: str, actor_id: str,
                      auth: str = Depends(token)) -> FileResponse:
        try:
            return FileResponse(portal.portrait_artwork(auth, game_id, actor_id))
        except (KeyError, PermissionError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/me/memory")
    async def player_memory(auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.memory_status(auth), name="memory-status")
        except ServiceStopping:
            raise
        except Exception as error:
            logger.exception("player memory status failed")
            raise HTTPException(status_code=503, detail="玩家画像暂时不可用") from error

    @app.post("/v1/me/memory/settings")
    async def set_player_memory(body: PlayerMemorySettingsBody,
                                auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.set_memory_enabled(auth, body.enabled), name="set-memory-enabled")
        except ValueError as error:
            raise _http_error(error) from error

    @app.delete("/v1/me/memory")
    async def clear_player_memory(auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.clear_memory(auth), name="clear-memory")
        except ValueError as error:
            raise _http_error(error) from error

    @app.patch("/v1/me/memory/{memory_id}")
    async def correct_player_memory(memory_id: str, body: PlayerMemoryCorrectionBody,
                                    auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.correct_memory(auth, memory_id, body.text), name="correct-memory")
        except (KeyError, ValueError) as error:
            raise _http_error(error) from error

    @app.delete("/v1/me/memory/{memory_id}")
    async def delete_player_memory(memory_id: str, auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.delete_memory(auth, memory_id), name="delete-memory")
        except (KeyError, ValueError) as error:
            raise _http_error(error) from error

    @app.get("/v1/my-scenarios")
    def my_scenarios(auth: str = Depends(token)) -> dict:
        return {"scenarios": portal.my_scenarios(auth)}

    async def read_scenario_archive(file: UploadFile) -> bytes:
        if file.content_type not in {"application/zip", "application/x-zip-compressed",
                                     "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="file must be a ZIP archive")
        content = bytearray()
        try:
            while chunk := await file.read(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_ARCHIVE_BYTES:
                    raise HTTPException(status_code=413, detail="ZIP file exceeds 4 MiB")
        finally:
            await file.close()
        return bytes(content)

    @app.post("/v1/my-scenarios", status_code=201)
    async def upload_scenario(title: str = Form(...), summary: str = Form(""),
                              file: UploadFile = File(...), auth: str = Depends(token)) -> dict:
        content = await read_scenario_archive(file)
        try:
            return await operations.run(
                lambda: portal.upload_scenario(auth, title, summary, content), name="upload")
        except ValueError as error:
            raise _http_error(error) from error
        except (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError) as error:
            raise _model_http_error(error) from error

    @app.post("/v1/my-scenarios/{scenario_id}/versions", status_code=201)
    async def upload_scenario_version(scenario_id: str, title: str = Form(...),
                                      summary: str = Form(""), file: UploadFile = File(...),
                                      auth: str = Depends(token)) -> dict:
        content = await read_scenario_archive(file)
        try:
            return await operations.run(
                lambda: portal.upload_scenario_version(auth, scenario_id, title, summary, content),
                name="upload-version")
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error
        except (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError) as error:
            raise _model_http_error(error) from error

    @app.post("/v1/my-scenarios/{scenario_id}/submit", status_code=201)
    def submit_scenario(scenario_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.submit_scenario(auth, scenario_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/my-submissions")
    def my_submissions(auth: str = Depends(token)) -> dict:
        return {"submissions": portal.my_submissions(auth)}

    @app.delete("/v1/my-submissions/{submission_id}")
    def withdraw_submission(submission_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.withdraw_submission(auth, submission_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/submissions")
    def review_queue(status: str = "pending", auth: str = Depends(token)) -> dict:
        try:
            return {"submissions": portal.review_queue(auth, status)}
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/submissions/{submission_id}")
    def review_detail(submission_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.review_detail(auth, submission_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/submissions/{submission_id}/preview", response_model=View, response_model_exclude_unset=True)
    async def review_preview(submission_id: str, auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.create_review_preview(auth, submission_id), name="create-review-preview")
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/submissions/{submission_id}/decision")
    def review_decide(submission_id: str, body: ReviewDecisionBody,
                      auth: str = Depends(token)) -> dict:
        try:
            return portal.review_decide(auth, submission_id, body.decision, body.reason)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/users")
    def admin_users(auth: str = Depends(token)) -> dict:
        try:
            return {"users": portal.admin_users(auth)}
        except PermissionError as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/invites")
    def admin_invitations(auth: str = Depends(token)) -> dict:
        try:
            return {"invites": portal.admin_invitations(auth)}
        except PermissionError as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/invites", status_code=201)
    def admin_issue_invitations(body: InvitationIssueBody,
                                auth: str = Depends(token)) -> dict:
        try:
            return portal.admin_issue_invitations(auth, body.count)
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/invites/{invitation_id}/revoke")
    def admin_revoke_invitation(invitation_id: str,
                                auth: str = Depends(token)) -> dict:
        try:
            return portal.admin_revoke_invitation(auth, invitation_id)
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/users/{player_id}/role")
    def admin_role(player_id: str, body: RoleBody, auth: str = Depends(token)) -> dict:
        try:
            return portal.admin_set_role(auth, player_id, body.role, body.enabled)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/users/{player_id}/status")
    def admin_status(player_id: str, body: AccountStatusBody,
                     auth: str = Depends(token)) -> dict:
        try:
            return portal.admin_set_status(auth, player_id, body.status)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/releases")
    def admin_releases(auth: str = Depends(token)) -> dict:
        try:
            return {"releases": portal.admin_releases(auth)}
        except PermissionError as error:
            raise _http_error(error) from error

    @app.post("/v1/manage/releases/{scenario_id}/state")
    def admin_release_state(scenario_id: str, body: ReleaseStateBody,
                            auth: str = Depends(token)) -> dict:
        try:
            return portal.admin_release_state(auth, scenario_id, body.state, body.reason)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/manage/audit")
    def admin_audit(auth: str = Depends(token)) -> dict:
        try:
            return {"events": portal.admin_audit(auth)}
        except PermissionError as error:
            raise _http_error(error) from error

    @app.post("/v1/my-scenarios/{scenario_id}/publish")
    def publish_scenario(scenario_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.publish_scenario(auth, scenario_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.delete("/v1/my-scenarios/{scenario_id}")
    def delete_scenario_draft(scenario_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.delete_scenario_draft(auth, scenario_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves")
    def saves(auth: str = Depends(token)) -> dict:
        try:
            return {"saves": portal.saves(auth)}
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/billing/wallet")
    def wallet(auth: str = Depends(token)) -> dict:
        try:
            return portal.wallet(auth)
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/billing/ledger")
    def credit_ledger(limit: int = 30, auth: str = Depends(token)) -> dict:
        try:
            return {"entries": portal.credit_ledger(auth, limit)}
        except (ValueError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/history", response_model=History, response_model_exclude_unset=True)
    def history(game_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.history(auth, game_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/saves", status_code=201, response_model=View, response_model_exclude_unset=True)
    async def create_save(body: SaveBody, auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.create_save(auth, body.catalog_id, body.play_mode,
                                            body.story_setup), name="create-save")
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/saves/{game_id}/resume", response_model=View, response_model_exclude_unset=True)
    async def resume(game_id: str, auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.resume_save(auth, game_id), name="resume-save")
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/settings")
    def save_settings(game_id: str, auth: str = Depends(token)) -> dict:
        try:
            return portal.get_save_settings(auth, game_id)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.put("/v1/saves/{game_id}/settings")
    async def update_save_settings(game_id: str, body: SaveSettingsBody,
                                   auth: str = Depends(token)) -> dict:
        try:
            return await operations.run(
                lambda: portal.set_save_settings(auth, game_id,
                                                  body.temperature, body.context_window_tokens), name="set-save-settings")
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.get("/v1/saves/{game_id}/turns/{request_id}/recovery", response_model=TurnRecoveryStatus)
    def turn_recovery_status(game_id: str,
                             request_id: str = Path(pattern=r"^[A-Za-z0-9_-]{1,64}$"),
                             input_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
                             auth: str = Depends(token)) -> dict:
        try:
            return portal.turn_recovery_status(auth, game_id, request_id, input_hash)
        except (ValueError, KeyError, PermissionError) as error:
            raise _http_error(error) from error

    @app.post("/v1/saves/{game_id}/turns", response_model=View, response_model_exclude_unset=True)
    async def turn(game_id: str, body: TurnBody, auth: str = Depends(token)) -> dict:
        if not operations.accepting:
            return JSONResponse(status_code=503, content=stopping_failure(body.request_id))
        try:
            view = await operations.run(
                lambda: portal.turn(auth, game_id, body.text, body.request_id), name="turn")
            return View.model_validate(view).model_dump(mode="json", exclude_unset=True)
        except ValidationError:
            logger.error("invalid completed turn response")
            failure = invalid_contract_failure(body.request_id)
            return JSONResponse(status_code=500, content=failure_content({"detail": failure["message"], **failure}))
        except ServiceStopping:
            return JSONResponse(status_code=503, content=stopping_failure(body.request_id))
        except (GenerationRequestLimitExceeded, GenerationTimeout,
                ExecutionRequestLimitExceeded, ExecutionTimeout) as error:
            failure = turn_failure(error, body.request_id)
            return JSONResponse(status_code=503,
                                content=failure_content({"detail": failure["message"], **failure}))
        except (ValueError, KeyError, PermissionError) as error:
            failure = turn_failure(error, body.request_id)
            return JSONResponse(status_code=_http_error(error).status_code,
                                content=failure_content({"detail": failure["message"], **failure}))
        except TRANSIENT_MODEL_ERRORS as error:
            logger.warning("model unavailable during portal turn: %s", type(error).__name__)
            mapped = _model_http_error(error)
            failure = turn_failure(error, body.request_id, message=str(mapped.detail),
                                   code="MODEL_UNAVAILABLE")
            return JSONResponse(status_code=mapped.status_code,
                                content=failure_content({"detail": failure["message"], **failure}))

    @app.post("/v1/saves/{game_id}/fact-queries", response_model=View,
              response_model_exclude_unset=True)
    async def fact_query(game_id: str, body: FactQueryBody,
                         auth: str = Depends(token)) -> dict:
        if not operations.accepting:
            return JSONResponse(status_code=503, content=stopping_failure(body.request_id))
        try:
            view = await operations.run(
                lambda: portal.fact_query(auth, game_id, body.request_id,
                                          body.fact_id, body.speaker_id), name="fact-query")
            return View.model_validate(view).model_dump(mode="json", exclude_unset=True)
        except ValidationError:
            logger.error("invalid completed fact query response")
            failure = invalid_contract_failure(body.request_id)
            return JSONResponse(status_code=500, content=failure_content({"detail": failure["message"], **failure}))
        except ServiceStopping:
            return JSONResponse(status_code=503, content=stopping_failure(body.request_id))
        except (ValueError, KeyError, PermissionError) as error:
            safe_message = (str(error) if isinstance(error, (TurnInputError, TurnRecoveryRequired,
                                                              KeyError, PermissionError))
                            else "事实查询未能确认；请使用原请求重试。")
            failure = turn_failure(error, body.request_id, message=safe_message)
            return JSONResponse(status_code=_http_error(error).status_code,
                                content=failure_content({"detail": failure["message"], **failure}))
        except Exception as error:
            logger.error("fact query failed (%s)", type(error).__name__)
            failure = turn_failure(error, body.request_id,
                                   message="事实查询未能确认；请使用原请求重试。")
            return JSONResponse(status_code=500,
                                content=failure_content({"detail": failure["message"], **failure}))

    @app.post("/v1/saves/{game_id}/turns/stream")
    async def stream_turn(game_id: str, body: TurnBody, auth: str = Depends(token)) -> StreamingResponse:
        if not operations.accepting:
            return JSONResponse(status_code=503, content=stopping_failure(body.request_id))

        async def events():
            # Headers and iteration are separate scheduling points. Recheck so
            # a response created just before shutdown cannot launch late work.
            if not operations.accepting:
                yield event_frame({"type": "error", **stopping_failure(body.request_id)})
                return
            queue: asyncio.Queue[dict | None] = asyncio.Queue()

            async def report(event: dict) -> None:
                await queue.put(event)

            async def run_turn() -> None:
                try:
                    view = await portal.turn(auth, game_id, body.text, body.request_id, progress=report)
                    await report({"type": "complete", "view": view})
                except (GenerationRequestLimitExceeded, GenerationTimeout,
                        ExecutionRequestLimitExceeded, ExecutionTimeout) as error:
                    await report({"type": "error", **turn_failure(error, body.request_id)})
                except (ValueError, KeyError, PermissionError) as error:
                    await report({"type": "error", **turn_failure(error, body.request_id)})
                except TRANSIENT_MODEL_ERRORS as error:
                    logger.warning("model unavailable during streamed turn: %s", type(error).__name__)
                    await report({"type": "error", **turn_failure(
                        error, body.request_id, message=_model_failure_message(error),
                        code="MODEL_UNAVAILABLE")})
                except Exception as error:
                    logger.exception("streamed turn failed")
                    await report({"type": "error", **turn_failure(
                        error, body.request_id, message="请求未能完成，请稍后重试。")})
                finally:
                    await queue.put(None)

            try:
                operations.start(run_turn, name="stream-turn")
            except ServiceStopping:
                yield event_frame({"type": "error", **stopping_failure(body.request_id)})
                return
            yield event_frame({"type": "stage", "stage": "received"})
            invalid = False
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                if invalid:
                    continue
                try:
                    frame = event_frame(event)
                except ValidationError:
                    # Progress callbacks may be swallowed by the runtime. Validate at
                    # dequeue, then drain supervised work without publishing a complete.
                    logger.error("invalid streamed turn response")
                    invalid = True
                    frame = event_frame({"type": "error", **invalid_contract_failure(body.request_id)})
                yield frame

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    return app


def serve(portal: PlayerPortal, host: str = "127.0.0.1", port: int = 8765) -> None:
    if portal.settings.environment == "local" and host not in {"127.0.0.1", "localhost"}:
        raise ValueError("local portal HTTP login must bind to localhost")
    import uvicorn

    uvicorn.run(create_app(portal), host=host, port=port, timeout_graceful_shutdown=30)
