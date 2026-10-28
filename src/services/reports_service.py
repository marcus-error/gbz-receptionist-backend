import csv
import io
from datetime import datetime, timedelta, timezone, date
from typing import Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.call_log import CallLog
from src.models.appointment import Appointment, AppointmentStatus


class ReportsService:
    def __init__(self, db: Session):
        self.db = db

    def get_date_bounds(self, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if period == "today":
            return today_start, now
        elif period == "yesterday":
            y_start = today_start - timedelta(days=1)
            return y_start, today_start
        elif period == "this_week":
            return today_start - timedelta(days=now.weekday()), now
        elif period == "this_month":
            return today_start.replace(day=1), now
        elif period == "this_quarter":
            q_month = 3 * ((now.month - 1) // 3) + 1
            return today_start.replace(month=q_month, day=1), now
        elif period == "half_year":
            return today_start - timedelta(days=180), now
        elif period == "this_year":
            return today_start.replace(month=1, day=1), now
        elif period == "custom" and start_date and end_date:
            s = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            e = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
            return s, e
        return None, None

    def _parse_duration_seconds(self, duration_str: str) -> int:
        """Parse MM:SS or HH:MM:SS duration string into total seconds."""
        try:
            parts = str(duration_str or "").strip().split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            pass
        return 0

    def _format_duration(self, total_seconds: int) -> str:
        """Format total seconds into MM:SS string."""
        mins = total_seconds // 60
        secs = total_seconds % 60
        return f"{mins:02d}:{secs:02d}"

    def get_overall_report(
        self,
        organization_id: Optional[int] = None,
        period: str = "this_month",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ):
        s_dt, e_dt = self.get_date_bounds(period, start_date, end_date)

        # 1. Call Log Query
        call_q = self.db.query(CallLog)
        if organization_id:
            call_q = call_q.filter(CallLog.organization_id == organization_id)
        if s_dt and e_dt:
            call_q = call_q.filter(CallLog.created_at >= s_dt, CallLog.created_at <= e_dt)

        calls = call_q.all()

        total_calls = len(calls)
        completed_calls = sum(1 for c in calls if "completed" in (c.status or "").lower() or "inbound" in (c.status or "").lower())
        answered_calls = sum(1 for c in calls if "ringing" not in (c.status or "").lower() and "failed" not in (c.status or "").lower())
        missed_calls = total_calls - answered_calls
        active_calls = sum(1 for c in calls if "ringing" in (c.status or "").lower() or "in-progress" in (c.status or "").lower())
        failed_calls = sum(1 for c in calls if "failed" in (c.status or "").lower())

        # Compute real average & total duration from parsed call records
        durations_seconds = [self._parse_duration_seconds(c.duration) for c in calls if c.duration]
        total_duration_secs = sum(durations_seconds)
        avg_duration_secs = (total_duration_secs // len(durations_seconds)) if durations_seconds else 0
        average_duration = self._format_duration(avg_duration_secs)
        total_duration_fmt = self._format_duration(total_duration_secs)

        # 2. Appointment Query
        appt_q = self.db.query(Appointment)
        if organization_id:
            appt_q = appt_q.filter(Appointment.organization_id == organization_id)
        if s_dt and e_dt:
            appt_q = appt_q.filter(Appointment.created_at >= s_dt, Appointment.created_at <= e_dt)

        appts = appt_q.all()

        total_appts = len(appts)
        today_str = date.today().strftime("%d %b %Y")
        today_appts = sum(1 for a in appts if a.appointment_date == today_str)
        confirmed_appts = sum(1 for a in appts if a.status in (AppointmentStatus.CONFIRMED, "Confirmed"))
        rescheduled_appts = sum(1 for a in appts if a.status in (AppointmentStatus.RESCHEDULED, "Rescheduled"))
        completed_appts = sum(1 for a in appts if a.status in (AppointmentStatus.COMPLETED, "Completed"))
        pending_appts = sum(1 for a in appts if a.status in (AppointmentStatus.PENDING, "Pending"))
        cancelled_appts = sum(1 for a in appts if a.status in (AppointmentStatus.CANCELLED, "Cancelled"))

        # 3. Detailed Lists
        call_details = [
            {
                "id": c.id,
                "caller": c.caller_name or "Unknown",
                "phone": c.caller_phone or "Unknown",
                "date": c.created_at.strftime("%Y-%m-%d") if c.created_at else "",
                "time": c.created_at.strftime("%H:%M:%S") if c.created_at else "",
                "duration": c.duration or "00:00",
                "status": c.status or "Completed",
                "intent": c.intent or "General Query",
                "emotion": c.emotion or "Neutral - 85%",
                "rag_used": bool(c.rag_source),
                "recording_available": bool(c.recording_url)
            }
            for c in calls
        ]

        appointment_details = [
            {
                "id": a.id,
                "customer": a.customer,
                "phone": a.phone or "",
                "department": a.department,
                "staff": a.staff,
                "date": a.appointment_date,
                "time": a.appointment_time,
                "status": str(a.status.value if hasattr(a.status, "value") else a.status),
                "notes": a.notes or ""
            }
            for a in appts
        ]

        return {
            "period": period,
            "call_summary": {
                "total_calls": total_calls,
                "answered_calls": answered_calls,
                "missed_calls": missed_calls,
                "completed_calls": completed_calls,
                "active_calls": active_calls,
                "failed_calls": failed_calls,
                "average_duration": average_duration,
                "total_duration": total_duration_fmt
            },
            "appointment_summary": {
                "total_appointments": total_appts,
                "today_appointments": today_appts,
                "scheduled_appointments": confirmed_appts,
                "rescheduled_appointments": rescheduled_appts,
                "cancelled_appointments": cancelled_appts,
                "completed_appointments": completed_appts if completed_appts > 0 else confirmed_appts,
                "pending_appointments": pending_appts
            },
            "daily_details": {
                "todays_calls": sum(1 for c in calls if c.created_at and c.created_at.strftime("%Y-%m-%d") == date.today().strftime("%Y-%m-%d")),
                "todays_scheduled_appointments": today_appts,
                "todays_cancelled_appointments": sum(1 for a in appts if a.appointment_date == today_str and a.status == AppointmentStatus.CANCELLED)
            },
            "call_details": call_details,
            "appointment_details": appointment_details
        }

    def generate_csv_report(
        self,
        organization_id: Optional[int] = None,
        period: str = "this_month",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> io.StringIO:
        report = self.get_overall_report(organization_id, period, start_date, end_date)
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(["=== AI RECEPTIONIST OPERATIONAL REPORT ==="])
        writer.writerow(["Period", report["period"]])
        writer.writerow([])

        writer.writerow(["=== CALL SUMMARY ==="])
        for k, v in report["call_summary"].items():
            writer.writerow([k.replace("_", " ").title(), v])
        writer.writerow([])

        writer.writerow(["=== APPOINTMENT SUMMARY ==="])
        for k, v in report["appointment_summary"].items():
            writer.writerow([k.replace("_", " ").title(), v])
        writer.writerow([])

        writer.writerow(["=== CALL DETAILS ==="])
        writer.writerow(["ID", "Caller", "Phone", "Date", "Time", "Duration", "Status", "Intent", "Emotion", "RAG Used"])
        for c in report["call_details"]:
            writer.writerow([c["id"], c["caller"], c["phone"], c["date"], c["time"], c["duration"], c["status"], c["intent"], c["emotion"], c["rag_used"]])
        writer.writerow([])

        writer.writerow(["=== APPOINTMENT DETAILS ==="])
        writer.writerow(["ID", "Customer", "Phone", "Department", "Staff", "Date", "Time", "Status"])
        for a in report["appointment_details"]:
            writer.writerow([a["id"], a["customer"], a["phone"], a["department"], a["staff"], a["date"], a["time"], a["status"]])

        output.seek(0)
        return output
