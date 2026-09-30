"""Tuition contracts and payment records (教培正式缴费/合同)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.lessons import ensure_hour_package, get_hour_balance
from backend.notify import create_notification, notify_parents_of_student


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days_until(value: Any) -> int | None:
    d = _as_date(value)
    if not d:
        return None
    return (d - datetime.now(CST).date()).days

router = APIRouter(tags=["billing"])
CST = timezone(timedelta(hours=8))


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def _d(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def _f(v: Any) -> float:
    return float(_d(v))


class ContractCreate(BaseModel):
    student_id: int
    title: str = Field(min_length=1, max_length=200)
    total_amount: float = Field(ge=0, le=1_000_000)
    hours_included: float = Field(default=0, ge=0, le=10_000)
    currency: str = Field(default="CNY", max_length=8)
    start_date: date | None = None
    end_date: date | None = None
    note: str | None = Field(default=None, max_length=500)
    activate: bool = True
    # 有金额合同时也可选择激活即先入账全部含课时（预授）
    grant_hours_on_activate: bool = False


class PaymentCreate(BaseModel):
    amount: float = Field(gt=0, le=1_000_000)
    method: str = Field(default="transfer", pattern="^(cash|wechat|alipay|transfer|other)$")
    paid_at: datetime | None = None
    hours_granted: float = Field(default=0, ge=0, le=10_000)
    note: str | None = Field(default=None, max_length=300)


def register_billing_routes(mysql_connection, current_user):
    @router.post("/api/contracts", status_code=201)
    def create_contract(payload: ContractCreate, user=Depends(current_user)):
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
        status = "active" if payload.activate else "draft"
        # 纯课时包（总额=0）或显式预授：激活即入账；否则等缴费按比例入账
        grant_now = (
            status == "active"
            and _d(payload.hours_included) > 0
            and (_d(payload.total_amount) <= 0 or payload.grant_hours_on_activate)
        )
        hours_granted = _d(payload.hours_included) if grant_now else _d(0)
        cursor.execute(
            "INSERT INTO tuition_contracts("
            "student_id,teacher_id,title,total_amount,currency,hours_included,hours_granted,status,start_date,end_date,note) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                payload.student_id, int(user["sub"]), payload.title.strip(),
                payload.total_amount, payload.currency, payload.hours_included,
                hours_granted, status, payload.start_date, payload.end_date, payload.note,
            ),
        )
        cid = cursor.lastrowid
        if grant_now:
            ensure_hour_package(cursor, payload.student_id, int(user["sub"]))
            cursor.execute(
                "UPDATE student_hour_packages SET total_hours=total_hours+%s WHERE student_id=%s",
                (hours_granted, payload.student_id),
            )
            cursor.execute(
                "INSERT INTO hour_transactions(student_id,delta_hours,reason,note,created_by) "
                "VALUES(%s,%s,'contract_grant',%s,%s)",
                (payload.student_id, hours_granted, f"合同#{cid}激活入账课时", int(user["sub"])),
            )
        bal = get_hour_balance(cursor, payload.student_id) if grant_now else None
        connection.commit(); cursor.close(); connection.close()
        body = f"已签订合同「{payload.title.strip()}」，总额 {payload.total_amount} {payload.currency}。"
        if grant_now:
            body += f" 已入账课时 {float(hours_granted)}。"
        create_notification(
            mysql_connection, payload.student_id, "学费合同已创建", body, "contract_created",
            "/learn.html", {"contract_id": cid},
        )
        notify_parents_of_student(
            mysql_connection, payload.student_id, "子女学费合同", body, "contract_created",
            "/learn.html", {"contract_id": cid, "student_id": payload.student_id},
        )
        return {"id": cid, "status": status, "hours_granted": float(hours_granted), "hour_balance": bal}

    @router.get("/api/contracts")
    def list_contracts(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        role = user["role"]
        if role in ("teacher", "admin"):
            sql = (
                "SELECT c.*,u.display_name AS student_name,t.display_name AS teacher_name "
                "FROM tuition_contracts c "
                "JOIN users u ON u.id=c.student_id JOIN users t ON t.id=c.teacher_id WHERE 1=1"
            )
            params: list[Any] = []
            if role != "admin":
                sql += " AND c.teacher_id=%s"
                params.append(uid)
            sql += " ORDER BY c.id DESC LIMIT 100"
            cursor.execute(sql, tuple(params))
        elif role == "student":
            cursor.execute(
                "SELECT c.*,u.display_name AS student_name,t.display_name AS teacher_name "
                "FROM tuition_contracts c "
                "JOIN users u ON u.id=c.student_id JOIN users t ON t.id=c.teacher_id "
                "WHERE c.student_id=%s ORDER BY c.id DESC LIMIT 50",
                (uid,),
            )
        elif role == "parent":
            cursor.execute(
                "SELECT c.*,u.display_name AS student_name,t.display_name AS teacher_name "
                "FROM tuition_contracts c "
                "JOIN users u ON u.id=c.student_id JOIN users t ON t.id=c.teacher_id "
                "JOIN parent_student_links p ON p.student_id=c.student_id AND p.parent_id=%s AND p.status='active' "
                "ORDER BY c.id DESC LIMIT 50",
                (uid,),
            )
        else:
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="insufficient role")
        items = cursor.fetchall()
        for row in items:
            row["total_amount"] = _f(row.get("total_amount"))
            row["paid_amount"] = _f(row.get("paid_amount"))
            row["remain_amount"] = round(_f(row.get("total_amount")) - _f(row.get("paid_amount")), 2)
            row["hours_included"] = _f(row.get("hours_included"))
            row["hours_granted"] = _f(row.get("hours_granted"))
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/contracts/{contract_id}/payments", status_code=201)
    def record_payment(contract_id: int, payload: PaymentCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("START TRANSACTION")
        except Exception:
            pass
        cursor.execute("SELECT * FROM tuition_contracts WHERE id=%s FOR UPDATE", (contract_id,))
        contract = cursor.fetchone()
        if not contract:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="contract not found")
        if user["role"] != "admin" and contract["teacher_id"] != int(user["sub"]):
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your contract")
        if contract["status"] == "cancelled":
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="contract cancelled")

        paid_at = payload.paid_at or datetime.now(CST).replace(tzinfo=None)
        if paid_at.tzinfo is not None:
            paid_at = paid_at.astimezone(CST).replace(tzinfo=None)
        amount = _d(payload.amount)
        if amount <= 0:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="amount must be positive")
        remain_amount = max(_d(0), _d(contract["total_amount"]) - _d(contract["paid_amount"]))
        if _d(contract["total_amount"]) > 0 and amount > remain_amount:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(
                status_code=400,
                detail=f"amount exceeds remain payable ({float(remain_amount)})",
            )
        hours = _d(payload.hours_granted)
        remain_hours = max(_d(0), _d(contract["hours_included"]) - _d(contract["hours_granted"]))
        # 未显式指定课时时，按合同剩余应授课时比例折算
        if hours <= 0 and _d(contract["hours_included"]) > 0 and _d(contract["total_amount"]) > 0:
            if remain_amount > 0 and remain_hours > 0:
                hours = (amount / remain_amount) * remain_hours
                hours = min(hours, remain_hours).quantize(Decimal("0.1"))
        # 显式赠课时也不可超过合同剩余应授
        if hours > remain_hours:
            hours = remain_hours

        cursor.execute(
            "INSERT INTO tuition_payments(contract_id,amount,method,paid_at,hours_granted,note,created_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (contract_id, amount, payload.method, paid_at, hours, payload.note, int(user["sub"])),
        )
        pid = cursor.lastrowid
        cursor.execute(
            "UPDATE tuition_contracts SET "
            "paid_amount=paid_amount+%s, hours_granted=hours_granted+%s, "
            "status=CASE "
            "  WHEN status='cancelled' THEN 'cancelled' "
            "  WHEN (paid_amount+%s) >= total_amount AND total_amount>0 THEN 'completed' "
            "  WHEN status='draft' THEN 'active' "
            "  ELSE status END "
            "WHERE id=%s",
            (amount, hours, amount, contract_id),
        )
        cursor.execute("SELECT * FROM tuition_contracts WHERE id=%s", (contract_id,))
        contract = cursor.fetchone()
        new_paid = _d(contract["paid_amount"])
        new_hours = _d(contract["hours_granted"])
        new_status = contract["status"]
        if hours > 0:
            ensure_hour_package(cursor, int(contract["student_id"]), int(user["sub"]))
            cursor.execute(
                "UPDATE student_hour_packages SET total_hours=total_hours+%s WHERE student_id=%s",
                (hours, int(contract["student_id"])),
            )
            cursor.execute(
                "INSERT INTO hour_transactions(student_id,delta_hours,reason,note,created_by) "
                "VALUES(%s,%s,'contract_pay',%s,%s)",
                (int(contract["student_id"]), hours, f"合同#{contract_id}缴费赠课时", int(user["sub"])),
            )
        bal = get_hour_balance(cursor, int(contract["student_id"])) if hours > 0 else None
        connection.commit(); cursor.close(); connection.close()
        body = f"合同「{contract['title']}」到账 {float(amount)}，累计已付 {float(new_paid)}。"
        if hours > 0:
            body += f" 入账课时 {float(hours)}。"
        create_notification(
            mysql_connection, int(contract["student_id"]), "缴费到账", body, "payment_recorded",
            "/learn.html", {"contract_id": contract_id, "payment_id": pid},
        )
        notify_parents_of_student(
            mysql_connection, int(contract["student_id"]), "子女缴费到账", body, "payment_recorded",
            "/learn.html", {"contract_id": contract_id, "student_id": int(contract["student_id"])},
        )
        return {
            "id": pid, "contract_id": contract_id, "amount": float(amount),
            "hours_granted": float(hours), "contract_status": new_status,
            "paid_amount": float(new_paid), "hour_balance": bal,
        }

    @router.post("/api/contracts/{contract_id}/cancel")
    def cancel_contract(contract_id: int, user=Depends(current_user)):
        """作废合同：冲回尚未消耗的已授课时（不超过当前剩余）。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM tuition_contracts WHERE id=%s FOR UPDATE", (contract_id,))
        contract = cursor.fetchone()
        if not contract:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="contract not found")
        if user["role"] != "admin" and contract["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your contract")
        if contract["status"] == "cancelled":
            cursor.close(); connection.close()
            return {"id": contract_id, "status": "cancelled", "already": True}
        sid = int(contract["student_id"])
        granted = _d(contract.get("hours_granted") or 0)
        clawback = _d(0)
        if granted > 0:
            bal = get_hour_balance(cursor, sid, for_update=True)
            clawback = min(granted, _d(bal["remain_hours"]))
            if clawback > 0:
                cursor.execute(
                    "UPDATE student_hour_packages SET total_hours=GREATEST(0,total_hours-%s) WHERE student_id=%s",
                    (clawback, sid),
                )
                cursor.execute(
                    "INSERT INTO hour_transactions(student_id,delta_hours,reason,note,created_by) "
                    "VALUES(%s,%s,'contract_cancel',%s,%s)",
                    (sid, -clawback, f"合同#{contract_id}作废冲回未用课时", int(user["sub"])),
                )
        cursor.execute(
            "UPDATE tuition_contracts SET status='cancelled', hours_granted=GREATEST(0,hours_granted-%s) WHERE id=%s",
            (clawback, contract_id),
        )
        bal_after = get_hour_balance(cursor, sid)
        connection.commit(); cursor.close(); connection.close()
        body = f"合同「{contract['title']}」已作废"
        if clawback > 0:
            body += f"，冲回未用课时 {float(clawback)}。"
        create_notification(
            mysql_connection, sid, "合同已作废", body, "contract_cancelled",
            "/learn.html", {"contract_id": contract_id},
        )
        notify_parents_of_student(
            mysql_connection, sid, "子女合同作废", body, "contract_cancelled",
            "/learn.html", {"contract_id": contract_id, "student_id": sid},
        )
        return {
            "id": contract_id, "status": "cancelled",
            "hours_clawed_back": float(clawback), "hour_balance": bal_after,
        }

    @router.get("/api/contracts/{contract_id}/payments")
    def list_payments(contract_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM tuition_contracts WHERE id=%s", (contract_id,))
        contract = cursor.fetchone()
        if not contract:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="contract not found")
        uid = int(user["sub"])
        role = user["role"]
        allowed = (
            role == "admin"
            or (role == "teacher" and contract["teacher_id"] == uid)
            or (role == "student" and contract["student_id"] == uid)
        )
        if role == "parent":
            cursor.execute(
                "SELECT 1 FROM parent_student_links WHERE parent_id=%s AND student_id=%s AND status='active'",
                (uid, contract["student_id"]),
            )
            allowed = bool(cursor.fetchone())
        if not allowed:
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="forbidden")
        cursor.execute(
            "SELECT * FROM tuition_payments WHERE contract_id=%s ORDER BY paid_at DESC, id DESC",
            (contract_id,),
        )
        items = cursor.fetchall()
        for row in items:
            row["amount"] = _f(row.get("amount"))
            row["hours_granted"] = _f(row.get("hours_granted"))
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.get("/api/billing/renewal-alerts")
    def renewal_alerts(
        hours_threshold: float = 3.0,
        days_ahead: int = 14,
        user=Depends(current_user),
    ):
        """续费/课时预警：低剩余课时、合同即将到期、未付清。"""
        hours_threshold = max(0.0, min(float(hours_threshold or 3), 100.0))
        days_ahead = max(1, min(int(days_ahead or 14), 90))
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        uid = int(user["sub"])
        role = user["role"]
        alerts: list[dict[str, Any]] = []

        if role in ("teacher", "admin"):
            # 教师名下学员：课时不足
            if role == "admin":
                cursor.execute(
                    "SELECT p.student_id,u.display_name AS student_name,p.total_hours,p.used_hours "
                    "FROM student_hour_packages p JOIN users u ON u.id=p.student_id "
                    "WHERE (p.total_hours - p.used_hours) <= %s ORDER BY (p.total_hours-p.used_hours) ASC LIMIT 50",
                    (hours_threshold,),
                )
            else:
                cursor.execute(
                    "SELECT DISTINCT p.student_id,u.display_name AS student_name,p.total_hours,p.used_hours "
                    "FROM student_hour_packages p "
                    "JOIN users u ON u.id=p.student_id "
                    "JOIN class_members m ON m.user_id=p.student_id "
                    "JOIN classes c ON c.id=m.class_id AND c.teacher_id=%s AND c.status='active' "
                    "WHERE (p.total_hours - p.used_hours) <= %s "
                    "ORDER BY (p.total_hours-p.used_hours) ASC LIMIT 50",
                    (uid, hours_threshold),
                )
            for row in cursor.fetchall():
                remain = _f(row["total_hours"]) - _f(row["used_hours"])
                alerts.append({
                    "type": "low_hours",
                    "severity": "high" if remain <= 1 else "medium",
                    "student_id": row["student_id"],
                    "student_name": row["student_name"],
                    "remain_hours": round(remain, 1),
                    "message": f"{row['student_name']} 剩余课时 {round(remain, 1)}，建议续费",
                })
            # 合同到期 / 未付清
            sql = (
                "SELECT c.id,c.title,c.student_id,c.end_date,c.total_amount,c.paid_amount,c.status,"
                "u.display_name AS student_name FROM tuition_contracts c "
                "JOIN users u ON u.id=c.student_id "
                "WHERE c.status IN ('active','draft') "
            )
            params: list[Any] = []
            if role != "admin":
                sql += " AND c.teacher_id=%s"
                params.append(uid)
            sql += " ORDER BY c.end_date IS NULL, c.end_date ASC LIMIT 80"
            cursor.execute(sql, tuple(params))
            for row in cursor.fetchall():
                remain_amt = round(_f(row["total_amount"]) - _f(row["paid_amount"]), 2)
                if remain_amt > 0 and _f(row["total_amount"]) > 0:
                    alerts.append({
                        "type": "unpaid",
                        "severity": "medium",
                        "contract_id": row["id"],
                        "student_id": row["student_id"],
                        "student_name": row["student_name"],
                        "remain_amount": remain_amt,
                        "message": f"合同「{row['title']}」待收 {remain_amt}（{row['student_name']}）",
                    })
                days_left = _days_until(row.get("end_date"))
                if days_left is not None:
                    if 0 <= days_left <= days_ahead:
                        alerts.append({
                            "type": "contract_expiring",
                            "severity": "high" if days_left <= 3 else "medium",
                            "contract_id": row["id"],
                            "student_id": row["student_id"],
                            "student_name": row["student_name"],
                            "days_left": days_left,
                            "end_date": str(row.get("end_date")),
                            "message": f"合同「{row['title']}」{days_left} 天后到期（{row['student_name']}）",
                        })
                    elif days_left < 0 and row["status"] == "active":
                        alerts.append({
                            "type": "contract_expired",
                            "severity": "high",
                            "contract_id": row["id"],
                            "student_id": row["student_id"],
                            "student_name": row["student_name"],
                            "days_left": days_left,
                            "end_date": str(row.get("end_date")),
                            "message": f"合同「{row['title']}」已过期（{row['student_name']}）",
                        })

        elif role == "student":
            bal = get_hour_balance(cursor, uid)
            if bal["remain_hours"] <= hours_threshold:
                alerts.append({
                    "type": "low_hours",
                    "severity": "high" if bal["remain_hours"] <= 1 else "medium",
                    "remain_hours": bal["remain_hours"],
                    "message": f"您的剩余课时为 {bal['remain_hours']}，请联系老师续费",
                })
            cursor.execute(
                "SELECT id,title,end_date,total_amount,paid_amount,status FROM tuition_contracts "
                "WHERE student_id=%s AND status IN ('active','draft') ORDER BY id DESC LIMIT 20",
                (uid,),
            )
            for row in cursor.fetchall():
                remain_amt = round(_f(row["total_amount"]) - _f(row["paid_amount"]), 2)
                if remain_amt > 0 and _f(row["total_amount"]) > 0:
                    alerts.append({
                        "type": "unpaid",
                        "severity": "medium",
                        "contract_id": row["id"],
                        "remain_amount": remain_amt,
                        "message": f"合同「{row['title']}」尚待支付 {remain_amt}",
                    })
                days_left = _days_until(row.get("end_date"))
                if days_left is not None and days_left <= days_ahead:
                    alerts.append({
                        "type": "contract_expiring" if days_left >= 0 else "contract_expired",
                        "severity": "high" if days_left <= 3 else "medium",
                        "contract_id": row["id"],
                        "days_left": days_left,
                        "end_date": str(row.get("end_date")),
                        "message": (
                            f"合同「{row['title']}」已过期"
                            if days_left < 0
                            else f"合同「{row['title']}」将在 {days_left} 天后到期"
                        ),
                    })

        elif role == "parent":
            cursor.execute(
                "SELECT p.student_id,u.display_name AS student_name,pkg.total_hours,pkg.used_hours "
                "FROM parent_student_links p "
                "JOIN users u ON u.id=p.student_id "
                "LEFT JOIN student_hour_packages pkg ON pkg.student_id=p.student_id "
                "WHERE p.parent_id=%s AND p.status='active'",
                (uid,),
            )
            for row in cursor.fetchall():
                remain = _f(row.get("total_hours")) - _f(row.get("used_hours"))
                if remain <= hours_threshold:
                    alerts.append({
                        "type": "low_hours",
                        "severity": "high" if remain <= 1 else "medium",
                        "student_id": row["student_id"],
                        "student_name": row["student_name"],
                        "remain_hours": round(remain, 1),
                        "message": f"子女 {row['student_name']} 剩余课时 {round(remain, 1)}，建议续费",
                    })

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        alerts.sort(key=lambda a: severity_rank.get(a.get("severity"), 9))
        cursor.close(); connection.close()
        return {
            "total": len(alerts),
            "hours_threshold": hours_threshold,
            "days_ahead": days_ahead,
            "items": alerts,
        }

    return router
