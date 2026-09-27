"""Class schedule (排课) + student hour packages (课时) + attendance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.notify import create_notification, notify_parents_of_student

router = APIRouter(tags=["lessons"])
CST = timezone(timedelta(hours=8))


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def _f(v: Any) -> float:
    return float(_dec(v))


def ensure_hour_package(cursor, student_id: int, created_by: int | None = None):
    cursor.execute(
        "INSERT IGNORE INTO student_hour_packages(student_id,total_hours,used_hours,created_by) "
        "VALUES(%s,0,0,%s)",
        (student_id, created_by),
    )


def get_hour_balance(cursor, student_id: int, for_update: bool = False) -> dict[str, Any]:
    ensure_hour_package(cursor, student_id)
    sql = (
        "SELECT student_id,total_hours,used_hours,note,updated_at FROM student_hour_packages WHERE student_id=%s"
    )
    if for_update:
        sql += " FOR UPDATE"
    cursor.execute(sql, (student_id,))
    row = cursor.fetchone() or {}
    total = _dec(row.get("total_hours"))
    used = _dec(row.get("used_hours"))
    return {
        "student_id": student_id,
        "total_hours": _f(total),
        "used_hours": _f(used),
        "remain_hours": _f(total - used),
        "note": row.get("note"),
        "updated_at": row.get("updated_at"),
    }


class LessonCreate(BaseModel):
    class_id: int
    title: str = Field(min_length=1, max_length=200)
    topic: str | None = Field(default=None, max_length=500)
    starts_at: datetime
    duration_minutes: int = Field(default=60, ge=15, le=480)
    hours_cost: float = Field(default=1.0, ge=0, le=50)


class LessonUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    topic: str | None = Field(default=None, max_length=500)
    starts_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=15, le=480)
    hours_cost: float | None = Field(default=None, ge=0, le=50)
    status: str | None = Field(default=None, pattern="^(scheduled|completed|cancelled)$")


class AttendanceItem(BaseModel):
    student_id: int
    status: str = Field(pattern="^(present|absent|leave)$")
    note: str | None = Field(default=None, max_length=200)


class AttendancePayload(BaseModel):
    items: list[AttendanceItem] = Field(min_length=1)


class HourPurchase(BaseModel):
    student_id: int
    hours: float = Field(gt=0, le=1000)
    note: str | None = Field(default=None, max_length=200)


def register_lesson_routes(mysql_connection, current_user):
    @router.post("/api/lessons", status_code=201)
    def create_lesson(payload: LessonCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute("SELECT id,teacher_id,status,name FROM classes WHERE id=%s", (payload.class_id,))
        clazz = cursor.fetchone()
        if not clazz or clazz["status"] != "active":
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and clazz["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        starts = payload.starts_at
        if starts.tzinfo is not None:
            starts = starts.astimezone(CST).replace(tzinfo=None)
        cursor.execute(
            "INSERT INTO lessons(class_id,teacher_id,title,topic,starts_at,duration_minutes,hours_cost) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                payload.class_id, int(user["sub"]), payload.title.strip(), payload.topic,
                starts, payload.duration_minutes, payload.hours_cost,
            ),
        )
        lid = cursor.lastrowid
        cursor.execute(
            "SELECT user_id FROM class_members WHERE class_id=%s AND member_role='student'",
            (payload.class_id,),
        )
        student_ids = [int(r["user_id"]) for r in cursor.fetchall()]
        connection.commit(); cursor.close(); connection.close()
        body = f"班级「{clazz['name']}」安排了课程「{payload.title.strip()}」，时间 {starts}。"
        for sid in student_ids:
            create_notification(
                mysql_connection, sid, "新课程安排", body, "lesson_scheduled",
                "/learn.html", {"lesson_id": lid, "class_id": payload.class_id},
            )
            notify_parents_of_student(
                mysql_connection, sid, "子女有新课程", body, "lesson_scheduled",
                "/learn.html", {"lesson_id": lid, "student_id": sid},
            )
        return {"id": lid, "class_id": payload.class_id, "title": payload.title.strip(), "starts_at": starts}

    @router.get("/api/lessons")
    def list_lessons(class_id: int | None = None, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        uid = int(user["sub"])
        role = user["role"]
        if role in ("teacher", "admin"):
            sql = (
                "SELECT l.*,c.name AS class_name FROM lessons l "
                "JOIN classes c ON c.id=l.class_id WHERE 1=1"
            )
            params: list[Any] = []
            if role != "admin":
                sql += " AND l.teacher_id=%s"
                params.append(uid)
            if class_id:
                sql += " AND l.class_id=%s"
                params.append(class_id)
            sql += " ORDER BY l.starts_at DESC LIMIT 100"
            cursor.execute(sql, tuple(params))
        elif role == "student":
            sql = (
                "SELECT l.*,c.name AS class_name FROM lessons l "
                "JOIN classes c ON c.id=l.class_id "
                "JOIN class_members cm ON cm.class_id=l.class_id AND cm.user_id=%s "
                "WHERE l.status!='cancelled'"
            )
            params = [uid]
            if class_id:
                sql += " AND l.class_id=%s"
                params.append(class_id)
            sql += " ORDER BY l.starts_at DESC LIMIT 100"
            cursor.execute(sql, tuple(params))
        elif role == "parent":
            sql = (
                "SELECT l.*,c.name AS class_name,u.id AS student_id,u.display_name AS student_name "
                "FROM lessons l "
                "JOIN classes c ON c.id=l.class_id "
                "JOIN class_members cm ON cm.class_id=l.class_id "
                "JOIN users u ON u.id=cm.user_id "
                "JOIN parent_student_links psl ON psl.student_id=u.id AND psl.parent_id=%s AND psl.status='active' "
                "WHERE l.status!='cancelled'"
            )
            params = [uid]
            if class_id:
                sql += " AND l.class_id=%s"
                params.append(class_id)
            sql += " ORDER BY l.starts_at DESC LIMIT 100"
            cursor.execute(sql, tuple(params))
        else:
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="insufficient role")
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.put("/api/lessons/{lesson_id}")
    def update_lesson(lesson_id: int, payload: LessonUpdate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM lessons WHERE id=%s", (lesson_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="lesson not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your lesson")
        fields, values = [], []
        if payload.title is not None:
            fields.append("title=%s"); values.append(payload.title.strip())
        if payload.topic is not None:
            fields.append("topic=%s"); values.append(payload.topic)
        if payload.starts_at is not None:
            starts = payload.starts_at
            if starts.tzinfo is not None:
                starts = starts.astimezone(CST).replace(tzinfo=None)
            fields.append("starts_at=%s"); values.append(starts)
        if payload.duration_minutes is not None:
            fields.append("duration_minutes=%s"); values.append(payload.duration_minutes)
        if payload.hours_cost is not None:
            fields.append("hours_cost=%s"); values.append(payload.hours_cost)
        if payload.status is not None:
            fields.append("status=%s"); values.append(payload.status)
        if not fields:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="no fields")
        refunded = 0
        if payload.status == "cancelled":
            # 取消已点名课程：冲正出席扣减
            cursor.execute(
                "SELECT student_id,hours_deducted FROM lesson_attendance "
                "WHERE lesson_id=%s AND hours_deducted>0",
                (lesson_id,),
            )
            for row in cursor.fetchall():
                hours = _dec(row["hours_deducted"])
                sid = int(row["student_id"])
                ensure_hour_package(cursor, sid, int(user["sub"]))
                cursor.execute(
                    "UPDATE student_hour_packages SET used_hours=GREATEST(0,used_hours-%s) WHERE student_id=%s",
                    (hours, sid),
                )
                cursor.execute(
                    "INSERT INTO hour_transactions(student_id,delta_hours,reason,ref_lesson_id,note,created_by) "
                    "VALUES(%s,%s,'cancel_refund',%s,%s,%s)",
                    (sid, hours, lesson_id, "取消课程退还课时", int(user["sub"])),
                )
                cursor.execute(
                    "UPDATE lesson_attendance SET hours_deducted=0,note=CONCAT(COALESCE(note,''),' [cancelled-refund]') "
                    "WHERE lesson_id=%s AND student_id=%s",
                    (lesson_id, sid),
                )
                refunded += 1
        values.append(lesson_id)
        cursor.execute(f"UPDATE lessons SET {', '.join(fields)} WHERE id=%s", tuple(values))
        connection.commit(); cursor.close(); connection.close()
        return {"id": lesson_id, "updated": True, "hours_refunded_students": refunded}

    @router.post("/api/lessons/{lesson_id}/attendance")
    def mark_attendance(lesson_id: int, payload: AttendancePayload, user=Depends(current_user)):
        """点名：出席学员扣减课时；已点名不可重复扣费。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("START TRANSACTION")
        except Exception:
            pass
        cursor.execute(
            "SELECT l.*,c.name AS class_name FROM lessons l JOIN classes c ON c.id=l.class_id WHERE l.id=%s FOR UPDATE",
            (lesson_id,),
        )
        lesson = cursor.fetchone()
        if not lesson:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="lesson not found")
        if user["role"] != "admin" and lesson["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your lesson")
        if lesson["status"] == "cancelled":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="cancelled lesson")

        cost = _dec(lesson.get("hours_cost") or 1)
        marked = 0
        deducted = 0
        # 事务提交后再通知，避免 rollback 后家长仍收到「已扣课时」
        pending_notices: list[tuple[int, float]] = []
        for item in payload.items:
            sid = int(item.student_id)
            cursor.execute(
                "SELECT 1 FROM class_members WHERE class_id=%s AND user_id=%s",
                (lesson["class_id"], sid),
            )
            if not cursor.fetchone():
                continue
            cursor.execute(
                "SELECT status,hours_deducted FROM lesson_attendance WHERE lesson_id=%s AND student_id=%s",
                (lesson_id, sid),
            )
            prev = cursor.fetchone()
            hours = cost if item.status == "present" else _dec(0)
            ensure_hour_package(cursor, sid, int(user["sub"]))
            if prev:
                prev_deducted = _dec(prev.get("hours_deducted") or 0)
                delta = hours - prev_deducted
                if delta > 0:
                    bal = get_hour_balance(cursor, sid, for_update=True)
                    if _dec(bal["remain_hours"]) < delta:
                        connection.rollback(); cursor.close(); connection.close()
                        raise HTTPException(
                            status_code=400,
                            detail=f"student {sid} insufficient hours (need {delta}, remain {bal['remain_hours']})",
                        )
                    cursor.execute(
                        "UPDATE student_hour_packages SET used_hours=used_hours+%s WHERE student_id=%s",
                        (delta, sid),
                    )
                    cursor.execute(
                        "INSERT INTO hour_transactions(student_id,delta_hours,reason,ref_lesson_id,note,created_by) "
                        "VALUES(%s,%s,'attendance_adjust',%s,%s,%s)",
                        (sid, -delta, lesson_id, f"补扣出席：{lesson['title']}", int(user["sub"])),
                    )
                    deducted += 1
                    pending_notices.append((sid, float(delta)))
                elif delta < 0:
                    refund = -delta
                    cursor.execute(
                        "UPDATE student_hour_packages SET used_hours=GREATEST(0,used_hours-%s) WHERE student_id=%s",
                        (refund, sid),
                    )
                    cursor.execute(
                        "INSERT INTO hour_transactions(student_id,delta_hours,reason,ref_lesson_id,note,created_by) "
                        "VALUES(%s,%s,'attendance_refund',%s,%s,%s)",
                        (sid, refund, lesson_id, f"出勤更正退还：{lesson['title']}", int(user["sub"])),
                    )
                cursor.execute(
                    "UPDATE lesson_attendance SET status=%s,hours_deducted=%s,note=%s,marked_at=NOW() "
                    "WHERE lesson_id=%s AND student_id=%s",
                    (item.status, hours, item.note, lesson_id, sid),
                )
                marked += 1
                continue
            if hours > 0:
                bal = get_hour_balance(cursor, sid, for_update=True)
                if _dec(bal["remain_hours"]) < hours:
                    connection.rollback(); cursor.close(); connection.close()
                    raise HTTPException(
                        status_code=400,
                        detail=f"student {sid} insufficient hours (need {hours}, remain {bal['remain_hours']})",
                    )
                cursor.execute(
                    "UPDATE student_hour_packages SET used_hours=used_hours+%s WHERE student_id=%s",
                    (hours, sid),
                )
                cursor.execute(
                    "INSERT INTO hour_transactions(student_id,delta_hours,reason,ref_lesson_id,note,created_by) "
                    "VALUES(%s,%s,'attendance',%s,%s,%s)",
                    (sid, -hours, lesson_id, f"出席扣减：{lesson['title']}", int(user["sub"])),
                )
                deducted += 1
                pending_notices.append((sid, float(hours)))
            cursor.execute(
                "INSERT INTO lesson_attendance(lesson_id,student_id,status,hours_deducted,note) "
                "VALUES(%s,%s,%s,%s,%s)",
                (lesson_id, sid, item.status, hours, item.note),
            )
            marked += 1

        cursor.execute("UPDATE lessons SET status='completed' WHERE id=%s AND status='scheduled'", (lesson_id,))
        connection.commit(); cursor.close(); connection.close()
        for sid, hours_f in pending_notices:
            create_notification(
                mysql_connection, sid, "课时已扣减",
                f"课程「{lesson['title']}」已记出席，扣减 {hours_f} 课时。",
                "hour_deducted", "/learn.html",
                {"lesson_id": lesson_id, "hours": hours_f},
            )
            notify_parents_of_student(
                mysql_connection, sid, "子女课时扣减",
                f"子女出席「{lesson['title']}」，扣减 {hours_f} 课时。",
                "hour_deducted", "/learn.html",
                {"lesson_id": lesson_id, "student_id": sid, "hours": hours_f},
            )
        return {"lesson_id": lesson_id, "marked": marked, "newly_deducted": deducted}

    @router.get("/api/lessons/{lesson_id}/attendance")
    def get_attendance(lesson_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id,class_id FROM lessons WHERE id=%s", (lesson_id,))
        lesson = cursor.fetchone()
        if not lesson:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="lesson not found")
        if user["role"] != "admin" and lesson["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your lesson")
        cursor.execute(
            "SELECT u.id AS student_id,u.display_name,u.username,"
            "la.status,la.hours_deducted,la.note,la.marked_at "
            "FROM class_members cm JOIN users u ON u.id=cm.user_id "
            "LEFT JOIN lesson_attendance la ON la.lesson_id=%s AND la.student_id=u.id "
            "WHERE cm.class_id=%s AND cm.member_role='student' ORDER BY u.id",
            (lesson_id, lesson["class_id"]),
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"lesson_id": lesson_id, "total": len(items), "items": items}

    @router.post("/api/hours/purchase")
    def purchase_hours(payload: HourPurchase, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,role,display_name FROM users WHERE id=%s", (payload.student_id,))
        stu = cursor.fetchone()
        if not stu or stu["role"] != "student":
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="student not found")
        if user["role"] != "admin":
            cursor.execute(
                "SELECT 1 FROM class_members cm "
                "JOIN classes c ON c.id=cm.class_id AND c.teacher_id=%s AND c.status='active' "
                "WHERE cm.user_id=%s AND cm.member_role='student' LIMIT 1",
                (int(user["sub"]), payload.student_id),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="student not in your classes")
        ensure_hour_package(cursor, payload.student_id, int(user["sub"]))
        hours = _dec(payload.hours)
        cursor.execute(
            "UPDATE student_hour_packages SET total_hours=total_hours+%s, note=COALESCE(%s,note) WHERE student_id=%s",
            (hours, payload.note, payload.student_id),
        )
        cursor.execute(
            "INSERT INTO hour_transactions(student_id,delta_hours,reason,note,created_by) "
            "VALUES(%s,%s,'purchase',%s,%s)",
            (payload.student_id, hours, payload.note, int(user["sub"])),
        )
        bal = get_hour_balance(cursor, payload.student_id)
        connection.commit(); cursor.close(); connection.close()
        create_notification(
            mysql_connection, payload.student_id, "课时已充值",
            f"已充值 {float(hours)} 课时，当前剩余 {bal['remain_hours']}。",
            "hour_purchase", "/learn.html", {"hours": float(hours)},
        )
        notify_parents_of_student(
            mysql_connection, payload.student_id, "子女课时充值",
            f"{stu['display_name']} 已充值 {float(hours)} 课时，剩余 {bal['remain_hours']}。",
            "hour_purchase", "/learn.html",
            {"student_id": payload.student_id, "hours": float(hours)},
        )
        return bal

    @router.get("/api/hours/students/{student_id}")
    def student_hours(student_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        role = user["role"]
        if role == "student" and uid != student_id:
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="forbidden")
        if role == "parent":
            cursor.execute(
                "SELECT 1 FROM parent_student_links WHERE parent_id=%s AND student_id=%s AND status='active'",
                (uid, student_id),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not linked")
        if role == "teacher":
            cursor.execute(
                "SELECT 1 FROM class_members cm "
                "JOIN classes c ON c.id=cm.class_id AND c.teacher_id=%s "
                "WHERE cm.user_id=%s AND cm.member_role='student' LIMIT 1",
                (uid, student_id),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="student not in your classes")
        elif role == "admin":
            pass
        elif role not in ("student", "parent"):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="insufficient role")
        bal = get_hour_balance(cursor, student_id)
        cursor.execute(
            "SELECT id,delta_hours,reason,ref_lesson_id,note,created_at "
            "FROM hour_transactions WHERE student_id=%s ORDER BY id DESC LIMIT 30",
            (student_id,),
        )
        tx = cursor.fetchall()
        cursor.close(); connection.close()
        return {**bal, "transactions": tx}

    @router.get("/api/hours/me")
    def my_hours(user=Depends(current_user)):
        require_role(user, "student")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        bal = get_hour_balance(cursor, int(user["sub"]))
        cursor.execute(
            "SELECT id,delta_hours,reason,ref_lesson_id,note,created_at "
            "FROM hour_transactions WHERE student_id=%s ORDER BY id DESC LIMIT 20",
            (int(user["sub"]),),
        )
        tx = cursor.fetchall()
        cursor.close(); connection.close()
        return {**bal, "transactions": tx}

    return router
