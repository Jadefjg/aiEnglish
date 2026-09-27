"""Oral 1v1 tutoring slot calendar and bookings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from decimal import Decimal

from backend.lessons import ensure_hour_package, get_hour_balance
from backend.notify import create_notification, notify_parents_of_student

router = APIRouter(tags=["booking"])
CST = timezone(timedelta(hours=8))


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(CST).replace(tzinfo=None)
    return dt


class SlotCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime
    topic: str | None = Field(default=None, max_length=200)


class SlotBatchCreate(BaseModel):
    slots: list[SlotCreate] = Field(min_length=1, max_length=40)


class BookPayload(BaseModel):
    note: str | None = Field(default=None, max_length=300)


class CompletePayload(BaseModel):
    hours_cost: float = Field(default=1.0, ge=0, le=50)


def register_booking_routes(mysql_connection, current_user):
    @router.post("/api/tutor/slots", status_code=201)
    def create_slots(payload: SlotBatchCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        created = []
        try:
            for slot in payload.slots:
                start = _naive(slot.starts_at)
                end = _naive(slot.ends_at)
                if end <= start:
                    raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
                if (end - start).total_seconds() > 4 * 3600:
                    raise HTTPException(status_code=400, detail="slot longer than 4 hours")
                # 同一教师时段重叠校验
                cursor.execute(
                    "SELECT id FROM tutor_slots WHERE teacher_id=%s AND status!='cancelled' "
                    "AND starts_at < %s AND ends_at > %s LIMIT 1",
                    (int(user["sub"]), end, start),
                )
                if cursor.fetchone():
                    raise HTTPException(status_code=400, detail=f"slot overlaps existing: {start} ~ {end}")
                cursor.execute(
                    "INSERT INTO tutor_slots(teacher_id,starts_at,ends_at,topic,status) VALUES(%s,%s,%s,%s,'open')",
                    (int(user["sub"]), start, end, slot.topic),
                )
                created.append(cursor.lastrowid)
            connection.commit()
        except HTTPException:
            connection.rollback(); raise
        except Exception:
            connection.rollback(); raise
        finally:
            cursor.close(); connection.close()
        return {"created": len(created), "ids": created}

    @router.get("/api/tutor/slots")
    def list_slots(
        teacher_id: int | None = None,
        only_open: bool = False,
        user=Depends(current_user),
    ):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        uid = int(user["sub"])
        role = user["role"]
        sql = (
            "SELECT s.*,u.display_name AS teacher_name,"
            "b.id AS booking_id,b.student_id,b.status AS booking_status,stu.display_name AS student_name "
            "FROM tutor_slots s "
            "JOIN users u ON u.id=s.teacher_id "
            "LEFT JOIN tutor_bookings b ON b.slot_id=s.id AND b.status!='cancelled' "
            "LEFT JOIN users stu ON stu.id=b.student_id WHERE s.status!='cancelled'"
        )
        params: list[Any] = []
        if role == "teacher":
            sql += " AND s.teacher_id=%s"
            params.append(uid)
        elif role == "admin" and teacher_id:
            sql += " AND s.teacher_id=%s"
            params.append(teacher_id)
        elif role == "student":
            if only_open:
                sql += " AND s.status='open'"
            else:
                sql += " AND (s.status='open' OR b.student_id=%s)"
                params.append(uid)
        elif role == "parent":
            sql += (
                " AND b.student_id IN ("
                "SELECT student_id FROM parent_student_links WHERE parent_id=%s AND status='active')"
            )
            params.append(uid)
        if only_open and role != "student":
            sql += " AND s.status='open'"
        sql += " AND s.starts_at >= DATE_SUB(NOW(), INTERVAL 1 DAY) ORDER BY s.starts_at LIMIT 120"
        cursor.execute(sql, tuple(params))
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/tutor/slots/{slot_id}/book", status_code=201)
    def book_slot(slot_id: int, payload: BookPayload, user=Depends(current_user)):
        require_role(user, "student")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute(
            "SELECT s.*,u.display_name AS teacher_name FROM tutor_slots s "
            "JOIN users u ON u.id=s.teacher_id WHERE s.id=%s",
            (slot_id,),
        )
        slot = cursor.fetchone()
        if not slot or slot["status"] != "open":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="slot not available")
        if slot["starts_at"] <= datetime.now(CST).replace(tzinfo=None):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="slot already started")
        uid = int(user["sub"])
        # 预约前校验剩余课时，避免约满后完成时才发现无课时
        bal = get_hour_balance(cursor, uid)
        if _dec(bal["remain_hours"]) <= 0:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="insufficient hours; please renew before booking")
        try:
            cursor.execute(
                "UPDATE tutor_slots SET status='booked' WHERE id=%s AND status='open'",
                (slot_id,),
            )
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="slot just booked by others")
            cursor.execute(
                "INSERT INTO tutor_bookings(slot_id,student_id,status,note) VALUES(%s,%s,'booked',%s)",
                (slot_id, uid, payload.note),
            )
            bid = cursor.lastrowid
            connection.commit()
        except HTTPException:
            connection.rollback(); cursor.close(); connection.close()
            raise
        except Exception:
            connection.rollback(); cursor.close(); connection.close()
            raise
        cursor.close(); connection.close()
        body = f"学生预约了口语 1v1：{slot['starts_at']} ~ {slot['ends_at']}"
        create_notification(
            mysql_connection, int(slot["teacher_id"]), "新的口语预约", body, "tutor_booked",
            "/learn.html", {"slot_id": slot_id, "booking_id": bid},
        )
        create_notification(
            mysql_connection, uid, "口语预约成功",
            f"已预约 {slot['teacher_name']}：{slot['starts_at']}",
            "tutor_booked", "/learn.html", {"slot_id": slot_id},
        )
        notify_parents_of_student(
            mysql_connection, uid, "子女口语预约",
            f"已预约老师 {slot['teacher_name']}，时间 {slot['starts_at']}",
            "tutor_booked", "/learn.html", {"slot_id": slot_id, "student_id": uid},
        )
        return {"booking_id": bid, "slot_id": slot_id, "status": "booked"}

    @router.post("/api/tutor/slots/{slot_id}/cancel")
    def cancel_open_slot(slot_id: int, user=Depends(current_user)):
        """教师取消未预约或已预约时段（已预约则同步取消 booking）。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM tutor_slots WHERE id=%s", (slot_id,))
        slot = cursor.fetchone()
        if not slot:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="slot not found")
        if user["role"] != "admin" and slot["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="forbidden")
        if slot["status"] in ("cancelled", "completed"):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="slot already closed")
        cursor.execute(
            "SELECT id,student_id FROM tutor_bookings WHERE slot_id=%s AND status='booked'",
            (slot_id,),
        )
        booked = cursor.fetchall()
        cursor.execute(
            "UPDATE tutor_bookings SET status='cancelled' WHERE slot_id=%s AND status='booked'",
            (slot_id,),
        )
        cursor.execute("UPDATE tutor_slots SET status='cancelled' WHERE id=%s", (slot_id,))
        connection.commit(); cursor.close(); connection.close()
        for b in booked:
            create_notification(
                mysql_connection, int(b["student_id"]), "口语时段已取消",
                f"教师取消了时段 {slot.get('starts_at')}，请重新预约。",
                "tutor_cancelled", "/learn.html", {"slot_id": slot_id},
            )
            notify_parents_of_student(
                mysql_connection, int(b["student_id"]), "子女口语预约取消",
                f"教师取消了时段 {slot.get('starts_at')}。",
                "tutor_cancelled", "/learn.html",
                {"slot_id": slot_id, "student_id": int(b["student_id"])},
            )
        return {"id": slot_id, "status": "cancelled", "notified_students": len(booked)}

    @router.post("/api/tutor/bookings/{booking_id}/cancel")
    def cancel_booking(booking_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT b.*,s.teacher_id,s.starts_at FROM tutor_bookings b "
            "JOIN tutor_slots s ON s.id=b.slot_id WHERE b.id=%s",
            (booking_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="booking not found")
        uid = int(user["sub"])
        role = user["role"]
        if role not in ("admin",) and uid not in (row["student_id"], row["teacher_id"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="forbidden")
        if row["status"] != "booked":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="booking not cancellable")
        cursor.execute("UPDATE tutor_bookings SET status='cancelled' WHERE id=%s", (booking_id,))
        cursor.execute("UPDATE tutor_slots SET status='open' WHERE id=%s", (row["slot_id"],))
        connection.commit(); cursor.close(); connection.close()
        other = int(row["teacher_id"] if uid == row["student_id"] else row["student_id"])
        create_notification(
            mysql_connection, other, "口语预约已取消",
            f"预约（{row.get('starts_at')}）已取消，时段已释放。",
            "tutor_cancelled", "/learn.html", {"booking_id": booking_id, "slot_id": row["slot_id"]},
        )
        if uid == row["teacher_id"] or role == "admin":
            notify_parents_of_student(
                mysql_connection, int(row["student_id"]), "子女口语预约取消",
                f"预约（{row.get('starts_at')}）已取消。",
                "tutor_cancelled", "/learn.html",
                {"booking_id": booking_id, "student_id": int(row["student_id"])},
            )
        return {"id": booking_id, "status": "cancelled"}

    @router.post("/api/tutor/bookings/{booking_id}/complete")
    def complete_booking(
        booking_id: int,
        payload: CompletePayload | None = None,
        user=Depends(current_user),
    ):
        """完成 1v1：扣课时 + 槽位标记 completed。"""
        require_role(user, "teacher", "admin")
        payload = payload or CompletePayload()
        hours = _dec(payload.hours_cost)
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT b.*,s.teacher_id,s.starts_at,s.ends_at,s.topic FROM tutor_bookings b "
            "JOIN tutor_slots s ON s.id=b.slot_id WHERE b.id=%s",
            (booking_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="booking not found")
        if user["role"] != "admin" and row["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="forbidden")
        if row["status"] != "booked":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="booking not active")
        sid = int(row["student_id"])
        try:
            cursor.execute("START TRANSACTION")
        except Exception:
            pass
        cursor.execute(
            "SELECT id,status FROM tutor_bookings WHERE id=%s FOR UPDATE",
            (booking_id,),
        )
        locked = cursor.fetchone()
        if not locked or locked["status"] != "booked":
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="booking not active")
        if hours > 0:
            ensure_hour_package(cursor, sid, int(user["sub"]))
            bal = get_hour_balance(cursor, sid, for_update=True)
            if _dec(bal["remain_hours"]) < hours:
                connection.rollback(); cursor.close(); connection.close()
                raise HTTPException(
                    status_code=400,
                    detail=f"insufficient hours (need {hours}, remain {bal['remain_hours']})",
                )
            cursor.execute(
                "UPDATE student_hour_packages SET used_hours=used_hours+%s WHERE student_id=%s",
                (hours, sid),
            )
            cursor.execute(
                "INSERT INTO hour_transactions(student_id,delta_hours,reason,note,created_by) "
                "VALUES(%s,%s,'tutor_1v1',%s,%s)",
                (sid, -hours, f"口语1v1#{booking_id}", int(user["sub"])),
            )
        cursor.execute("UPDATE tutor_bookings SET status='completed' WHERE id=%s", (booking_id,))
        cursor.execute("UPDATE tutor_slots SET status='completed' WHERE id=%s", (row["slot_id"],))
        bal_after = get_hour_balance(cursor, sid)
        connection.commit(); cursor.close(); connection.close()
        body = f"老师已确认本次 1v1 口语课完成，扣减 {float(hours)} 课时。"
        create_notification(
            mysql_connection, sid, "口语课已完成", body, "tutor_completed",
            "/learn.html", {"booking_id": booking_id, "hours": float(hours)},
        )
        notify_parents_of_student(
            mysql_connection, sid, "子女口语课完成", body, "tutor_completed",
            "/learn.html", {"booking_id": booking_id, "student_id": sid, "hours": float(hours)},
        )
        return {
            "id": booking_id, "status": "completed",
            "hours_cost": float(hours), "hour_balance": bal_after,
        }

    return router
