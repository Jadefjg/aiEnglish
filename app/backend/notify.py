"""In-app notifications + optional WeChat template / generic webhook push."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["notifications"])

_wechat_token_cache: dict[str, Any] = {"token": "", "exp": 0.0}
_lock = threading.Lock()


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required for notifications")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def notify_status() -> dict[str, Any]:
    return {
        "inbox": True,
        "webhook_global": bool(os.getenv("NOTIFY_WEBHOOK_URL", "").strip()),
        "wechat_oa": bool(
            os.getenv("WECHAT_APP_ID", "").strip()
            and os.getenv("WECHAT_APP_SECRET", "").strip()
            and os.getenv("WECHAT_TEMPLATE_ID", "").strip()
        ),
        "note": "站内信始终可用；配置 NOTIFY_WEBHOOK_URL 或微信公众号模板后可外发",
    }


def _post_json(url: str, payload: dict[str, Any], timeout: int = 8) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return True, body[:300]
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='ignore')[:200]}"
    except Exception as exc:
        return False, str(exc)


def _wechat_access_token() -> str:
    app_id = os.getenv("WECHAT_APP_ID", "").strip()
    secret = os.getenv("WECHAT_APP_SECRET", "").strip()
    if not app_id or not secret:
        raise RuntimeError("WeChat OA not configured")
    now = time.time()
    with _lock:
        if _wechat_token_cache["token"] and _wechat_token_cache["exp"] > now + 60:
            return _wechat_token_cache["token"]
    query = urllib.parse.urlencode(
        {"grant_type": "client_credential", "appid": app_id, "secret": secret}
    )
    url = f"https://api.weixin.qq.com/cgi-bin/token?{query}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"WeChat token error: {payload}")
    with _lock:
        _wechat_token_cache["token"] = token
        _wechat_token_cache["exp"] = now + int(payload.get("expires_in") or 7200)
    return token


def _send_wechat_template(openid: str, title: str, body: str, link_url: str | None) -> tuple[bool, str]:
    template_id = os.getenv("WECHAT_TEMPLATE_ID", "").strip()
    if not template_id:
        return False, "WECHAT_TEMPLATE_ID missing"
    token = _wechat_access_token()
    # 通用模板字段：thing/first/remark 兼容常见模板；可按需改环境变量映射
    payload = {
        "touser": openid,
        "template_id": template_id,
        "url": link_url or os.getenv("WECHAT_DEFAULT_URL", "").strip() or None,
        "data": {
            "thing1": {"value": title[:20]},
            "thing2": {"value": body[:20]},
            "time3": {"value": time.strftime("%Y-%m-%d %H:%M:%S")},
        },
    }
    # 允许完整自定义 data JSON
    custom = os.getenv("WECHAT_TEMPLATE_DATA_JSON", "").strip()
    if custom:
        try:
            data_tpl = json.loads(custom)
            rendered = {}
            for k, v in data_tpl.items():
                if isinstance(v, str):
                    rendered[k] = {"value": v.format(title=title[:20], body=body[:20])[:20]}
                else:
                    rendered[k] = v
            payload["data"] = rendered
        except Exception:
            pass
    url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}"
    return _post_json(url, payload)


def create_notification(
    mysql_connection: Callable,
    user_id: int,
    title: str,
    body: str,
    category: str = "general",
    link_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    connection = mysql_connection()
    if not connection:
        return None
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "INSERT INTO notifications(user_id,title,body,category,link_url,payload_json,channel_status) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                user_id, title[:200], body[:1000], category[:40], link_url,
                json.dumps(payload or {}, ensure_ascii=False), "inbox",
            ),
        )
        nid = cursor.lastrowid
        cursor.execute(
            "SELECT wechat_openid, notify_webhook, role, display_name FROM users WHERE id=%s",
            (user_id,),
        )
        user = cursor.fetchone() or {}
        connection.commit()
    except Exception:
        connection.rollback()
        cursor.close(); connection.close()
        return None
    cursor.close(); connection.close()

    channels = ["inbox"]
    # per-user webhook
    hooks = []
    global_hook = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if global_hook:
        hooks.append(global_hook)
    if user.get("notify_webhook"):
        hooks.append(user["notify_webhook"])
    event = {
        "id": nid,
        "user_id": user_id,
        "role": user.get("role"),
        "display_name": user.get("display_name"),
        "title": title,
        "body": body,
        "category": category,
        "link_url": link_url,
        "payload": payload or {},
    }
    for hook in hooks:
        ok, _ = _post_json(hook, event)
        if ok:
            channels.append("webhook")

    openid = (user.get("wechat_openid") or "").strip()
    if openid and notify_status()["wechat_oa"]:
        try:
            ok, detail = _send_wechat_template(openid, title, body, link_url)
            if ok:
                channels.append("wechat")
            else:
                channels.append("wechat_failed:" + detail[:40])
        except Exception as exc:
            channels.append("wechat_error:" + str(exc)[:40])

    # best-effort channel status update
    connection = mysql_connection()
    if connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE notifications SET channel_status=%s WHERE id=%s",
                (",".join(dict.fromkeys(channels))[:200], nid),
            )
            connection.commit()
        except Exception:
            connection.rollback()
        finally:
            cursor.close(); connection.close()
    return nid


def notify_parents_of_student(
    mysql_connection: Callable,
    student_id: int,
    title: str,
    body: str,
    category: str,
    link_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    connection = mysql_connection()
    if not connection:
        return 0
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        "SELECT parent_id FROM parent_student_links WHERE student_id=%s AND status='active'",
        (student_id,),
    )
    parents = [r["parent_id"] for r in cursor.fetchall()]
    cursor.close(); connection.close()
    count = 0
    for pid in parents:
        if create_notification(mysql_connection, int(pid), title, body, category, link_url, payload):
            count += 1
    return count


def notify_users(
    mysql_connection: Callable,
    user_ids: list[int],
    title: str,
    body: str,
    category: str,
    link_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    count = 0
    for uid in dict.fromkeys(int(x) for x in user_ids):
        if create_notification(mysql_connection, uid, title, body, category, link_url, payload):
            count += 1
    return count


def notify_assignment_published(
    mysql_connection: Callable,
    assignment_id: int,
    title: str,
    student_ids: list[int],
) -> dict[str, int]:
    link = "/learn.html"
    body = f"新作业「{title}」已发布，请尽快完成。"
    students = notify_users(
        mysql_connection, student_ids, "作业已发布", body, "assignment_published", link,
        {"assignment_id": assignment_id},
    )
    parents = 0
    for sid in student_ids:
        parents += notify_parents_of_student(
            mysql_connection, int(sid), "子女有新作业", body, "assignment_published", link,
            {"assignment_id": assignment_id, "student_id": int(sid)},
        )
    return {"students": students, "parents": parents}


def notify_assignment_urged(
    mysql_connection: Callable,
    assignment_id: int,
    title: str,
    student_ids: list[int],
) -> dict[str, int]:
    link = "/learn.html"
    body = f"教师催交：作业「{title}」尚未完成，请尽快提交。"
    students = notify_users(
        mysql_connection, student_ids, "作业催交提醒", body, "assignment_urge", link,
        {"assignment_id": assignment_id},
    )
    parents = 0
    for sid in student_ids:
        parents += notify_parents_of_student(
            mysql_connection, int(sid), "子女作业催交", body, "assignment_urge", link,
            {"assignment_id": assignment_id, "student_id": int(sid)},
        )
    return {"students": students, "parents": parents}


class ChannelUpdate(BaseModel):
    wechat_openid: str | None = Field(default=None, max_length=64)
    notify_webhook: str | None = Field(default=None, max_length=500)


def register_notify_routes(mysql_connection, current_user):
    @router.get("/api/notifications/status")
    def status(user=Depends(current_user)):
        return notify_status()

    @router.get("/api/notifications")
    def list_notifications(unread_only: bool = False, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        sql = (
            "SELECT id,title,body,category,link_url,payload_json,channel_status,is_read,created_at "
            "FROM notifications WHERE user_id=%s"
        )
        params: list[Any] = [int(user["sub"])]
        if unread_only:
            sql += " AND is_read=0"
        sql += " ORDER BY id DESC LIMIT 50"
        cursor.execute(sql, tuple(params))
        items = cursor.fetchall()
        for row in items:
            raw = row.pop("payload_json", None)
            if isinstance(raw, (dict, list)):
                row["payload"] = raw
            elif raw:
                try:
                    row["payload"] = json.loads(raw)
                except Exception:
                    row["payload"] = {}
            else:
                row["payload"] = {}
        cursor.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id=%s AND is_read=0",
            (int(user["sub"]),),
        )
        unread = int((cursor.fetchone() or {}).get("c") or 0)
        cursor.close(); connection.close()
        return {"total": len(items), "unread": unread, "items": items, "channels": notify_status()}

    @router.post("/api/notifications/{notification_id}/read")
    def mark_read(notification_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s",
            (notification_id, int(user["sub"])),
        )
        connection.commit(); cursor.close(); connection.close()
        return {"id": notification_id, "is_read": 1}

    @router.post("/api/notifications/read-all")
    def mark_all_read(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read=1 WHERE user_id=%s AND is_read=0",
            (int(user["sub"]),),
        )
        n = cursor.rowcount
        connection.commit(); cursor.close(); connection.close()
        return {"updated": n}

    @router.put("/api/notifications/channels")
    def update_channels(payload: ChannelUpdate, user=Depends(current_user)):
        require_role(user, "parent", "teacher", "student", "admin")
        fields, values = [], []
        if payload.wechat_openid is not None:
            fields.append("wechat_openid=%s")
            values.append(payload.wechat_openid.strip() or None)
        if payload.notify_webhook is not None:
            fields.append("notify_webhook=%s")
            values.append(payload.notify_webhook.strip() or None)
        if not fields:
            raise HTTPException(status_code=400, detail="no channel fields")
        values.append(int(user["sub"]))
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=%s", tuple(values))
        connection.commit(); cursor.close(); connection.close()
        return {"updated": True, "channels": notify_status()}

    return router
