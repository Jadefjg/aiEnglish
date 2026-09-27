"""Server-side media watch progress sessions (anti-cheat for video/listen)."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["media-progress"])
CST = timezone(timedelta(hours=8))


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def _hash_token(token: str) -> str:
    secret = os.getenv("JWT_SECRET", "dev-only-change-this-secret").encode()
    return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()


class SessionStart(BaseModel):
    assignment_id: int
    task_id: int
    media_key: str = Field(min_length=1, max_length=300)
    duration_seconds: int = Field(default=0, ge=0, le=24 * 3600)


class HeartbeatPayload(BaseModel):
    session_id: int
    token: str = Field(min_length=8, max_length=128)
    position: float = Field(ge=0, le=24 * 3600)
    delta: float = Field(ge=0, le=120)


def register_media_progress_routes(mysql_connection, current_user):
    @router.post("/api/media/session/start", status_code=201)
    def start_session(payload: SessionStart, user=Depends(current_user)):
        require_role(user, "student", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        cursor.execute(
            "SELECT a.id,a.status,t.id AS task_id,t.task_type,t.config_json "
            "FROM assignments a JOIN assignment_tasks t ON t.assignment_id=a.id "
            "WHERE a.id=%s AND t.id=%s",
            (payload.assignment_id, payload.task_id),
        )
        row = cursor.fetchone()
        if not row or row["status"] != "published":
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="task not available")
        if row["task_type"] not in {"video", "listen"}:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="only video/listen tasks need media sessions")
        if user["role"] == "student":
            cursor.execute(
                "SELECT 1 FROM assignment_students WHERE assignment_id=%s AND student_id=%s",
                (payload.assignment_id, uid),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not assigned")
        token = secrets.token_urlsafe(24)
        expires = datetime.now(CST).replace(tzinfo=None) + timedelta(hours=6)
        cursor.execute(
            "INSERT INTO media_progress_sessions("
            "user_id,assignment_id,task_id,media_key,duration_seconds,token_hash,expires_at,last_heartbeat_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())",
            (
                uid, payload.assignment_id, payload.task_id, payload.media_key[:300],
                int(payload.duration_seconds or 0), _hash_token(token), expires,
            ),
        )
        sid = cursor.lastrowid
        connection.commit(); cursor.close(); connection.close()
        return {
            "session_id": sid,
            "token": token,
            "expires_at": expires.isoformat(sep=" "),
            "rules": {
                "max_speed": 1.35,
                "max_jump_seconds": 8,
                "note": "heartbeat delta cannot exceed wall-clock*1.35; position jump limited",
            },
        }

    @router.post("/api/media/session/heartbeat")
    def heartbeat(payload: HeartbeatPayload, user=Depends(current_user)):
        require_role(user, "student", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute(
            "SELECT * FROM media_progress_sessions WHERE id=%s AND user_id=%s",
            (payload.session_id, int(user["sub"])),
        )
        sess = cursor.fetchone()
        if not sess:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="session not found")
        if sess["expires_at"] and sess["expires_at"] < datetime.now(CST).replace(tzinfo=None):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="session expired")
        if not hmac.compare_digest(sess["token_hash"], _hash_token(payload.token)):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="invalid session token")

        now = datetime.now(CST).replace(tzinfo=None)
        last = sess.get("last_heartbeat_at") or sess.get("created_at") or now
        if hasattr(last, "tzinfo") and last.tzinfo is not None:
            last = last.astimezone(CST).replace(tzinfo=None)
        elapsed = max(0.0, (now - last).total_seconds())
        # 允许轻微时钟抖动；拒绝远超真实流逝的刷进度
        max_delta = max(1.0, elapsed * 1.35)
        accepted_delta = min(float(payload.delta), max_delta, 30.0)
        prev_pos = float(sess.get("max_position") or 0)
        # 禁止大幅跳跃拖动进度条
        if payload.position > prev_pos + max(8.0, elapsed * 1.5 + 2):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="position jump too large")
        duration = float(sess.get("duration_seconds") or 0)
        position = float(payload.position)
        if duration > 0:
            position = min(position, duration + 1)
        new_max = max(prev_pos, position)
        new_cum = float(sess.get("cumulative_watch") or 0) + accepted_delta
        if duration > 0:
            new_cum = min(new_cum, duration + 2)
        cursor.execute(
            "UPDATE media_progress_sessions SET max_position=%s, cumulative_watch=%s, last_heartbeat_at=NOW() "
            "WHERE id=%s",
            (new_max, new_cum, payload.session_id),
        )
        connection.commit(); cursor.close(); connection.close()
        return {
            "session_id": payload.session_id,
            "accepted_delta": round(accepted_delta, 2),
            "max_position": round(new_max, 2),
            "cumulative_watch": round(new_cum, 2),
            "eligible_seconds": int(new_cum),
        }

    @router.get("/api/media/session/{session_id}")
    def get_session(session_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id,assignment_id,task_id,media_key,duration_seconds,max_position,cumulative_watch,expires_at "
            "FROM media_progress_sessions WHERE id=%s AND user_id=%s",
            (session_id, int(user["sub"])),
        )
        row = cursor.fetchone()
        cursor.close(); connection.close()
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        row["max_position"] = float(row.get("max_position") or 0)
        row["cumulative_watch"] = float(row.get("cumulative_watch") or 0)
        return row

    return router


def verify_media_progress(
    cursor,
    user_id: int,
    assignment_id: int,
    task_id: int,
    session_id: int | None,
    token: str | None,
    claimed_seconds: int,
) -> int:
    """Return trusted watched seconds for grading; raise HTTPException if invalid."""
    if not session_id or not token:
        raise HTTPException(status_code=400, detail="media_session_id and media_token required for video/listen")
    cursor.execute(
        "SELECT * FROM media_progress_sessions WHERE id=%s AND user_id=%s "
        "AND assignment_id=%s AND task_id=%s",
        (session_id, user_id, assignment_id, task_id),
    )
    sess = cursor.fetchone()
    if not sess:
        raise HTTPException(status_code=400, detail="media session not found")
    if not hmac.compare_digest(sess["token_hash"], _hash_token(token)):
        raise HTTPException(status_code=403, detail="invalid media token")
    trusted = int(float(sess.get("cumulative_watch") or 0))
    # 客户端上报只能更小，不能更大
    return max(0, min(int(claimed_seconds or 0), trusted))
