"""
Admin audit log read endpoints.
GET /admin/audit/logs
GET /admin/audit/alerts
GET /admin/audit/export
"""

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

import csv
import io

from database import get_db
from deps.admin_auth import CurrentAdmin

router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


@router.get("/logs")
async def get_audit_logs(
    admin: CurrentAdmin,
    category: Optional[str] = None,
    action: Optional[str] = None,
    admin_email: Optional[str] = None,
    result: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    db = get_db()
    query: dict = {}
    if category:
        query["category"] = category
    if action:
        query["action"] = action
    if admin_email:
        query["admin_email"] = admin_email
    if result:
        query["result"] = result

    skip = (page - 1) * page_size
    cursor = db.admin_audit_log.find(query, {"_id": 0}).sort("timestamp", -1).skip(skip).limit(page_size)
    logs = await cursor.to_list(page_size)
    total = await db.admin_audit_log.count_documents(query)

    # Serialise datetime objects
    for log in logs:
        if hasattr(log.get("timestamp"), "isoformat"):
            log["timestamp"] = log["timestamp"].isoformat()

    return {"logs": logs, "total": total, "page": page, "page_size": page_size}


@router.get("/alerts")
async def get_security_alerts(admin: CurrentAdmin):
    """Return active security alerts (recent suspicious events)."""
    db = get_db()
    # Failed login events in the last hour
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(hours=1)

    failed_logins = await db.admin_audit_log.count_documents({
        "action": "admin_login",
        "result": "failure",
        "timestamp": {"$gte": since},
    })

    alerts = []
    if failed_logins >= 3:
        alerts.append({
            "type": "failed_logins",
            "message": f"{failed_logins} failed admin login attempts in the last hour",
            "severity": "warning" if failed_logins < 5 else "critical",
        })

    return alerts


@router.get("/export")
async def export_audit_csv(admin: CurrentAdmin):
    """Stream audit log as CSV."""
    db = get_db()
    docs = await db.admin_audit_log.find({}, {"_id": 0}).sort("timestamp", -1).limit(5000).to_list(5000)

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["timestamp", "admin_email", "admin_ip", "action", "category",
                    "target_type", "target_id", "result", "error_message"],
        extrasaction="ignore",
    )
    writer.writeheader()
    for doc in docs:
        if hasattr(doc.get("timestamp"), "isoformat"):
            doc["timestamp"] = doc["timestamp"].isoformat()
        writer.writerow(doc)

    output.seek(0)
    return StreamingResponse(
        iter([output.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
