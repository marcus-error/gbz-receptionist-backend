import io
import json
import os
import wave
import zipfile
from datetime import datetime, timedelta, timezone, date
from typing import List, Optional, Tuple
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.models.call_log import CallLog, CallType
from src.models.appointment import Appointment


class RecordingService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def resolve_recording_disk_path(log: CallLog) -> Optional[str]:
        """Resolve actual existing WAV file path on persistent storage for this CallLog."""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        rec_dir = os.path.join(base_dir, "uploads", "recordings")

        # 1. Check by explicit recording_url
        if log.recording_url:
            rel = log.recording_url.lstrip("/\\")
            candidate = os.path.abspath(os.path.join(base_dir, rel))
            if os.path.exists(candidate) and os.path.getsize(candidate) > 44:
                return candidate

        # 2. Check by call_sid in uploads/recordings
        if log.call_sid:
            candidate = os.path.join(rec_dir, f"{log.call_sid}.wav")
            if os.path.exists(candidate) and os.path.getsize(candidate) > 44:
                return candidate

        return None

    def get_date_range(self, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if period == "daily":
            return today_start, now
        elif period == "weekly":
            return today_start - timedelta(days=now.weekday()), now
        elif period == "monthly":
            return today_start.replace(day=1), now
        elif period == "quarterly":
            quarter_month = 3 * ((now.month - 1) // 3) + 1
            return today_start.replace(month=quarter_month, day=1), now
        elif period == "half_yearly":
            return today_start - timedelta(days=180), now
        elif period == "annually":
            return today_start.replace(month=1, day=1), now
        elif period == "custom" and start_date and end_date:
            s = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            e = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
            return s, e
        return None, None

    def list_recordings(
        self,
        organization_id: Optional[int] = None,
        period: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        search: Optional[str] = None,
        call_type: Optional[str] = None,
    ):
        query = self.db.query(CallLog)

        if organization_id:
            query = query.filter(CallLog.organization_id == organization_id)

        # Call type filtering
        if call_type and call_type.lower() != "all":
            ct = call_type.strip().lower()
            if ct in ("inbound", "in"):
                query = query.filter(CallLog.call_type == CallType.INBOUND)
            elif ct in ("outbound", "out"):
                query = query.filter(CallLog.call_type == CallType.OUTBOUND)
            elif ct in ("web_test", "voice_test", "test"):
                query = query.filter(CallLog.call_type == CallType.VOICE_TEST)

        s_dt, e_dt = self.get_date_range(period or "monthly", start_date, end_date)
        if s_dt and e_dt:
            query = query.filter(CallLog.created_at >= s_dt, CallLog.created_at <= e_dt)

        if search:
            s_term = f"%{search}%"
            query = query.filter(
                or_(
                    CallLog.caller_name.ilike(s_term),
                    CallLog.caller_phone.ilike(s_term),
                    CallLog.intent.ilike(s_term)
                )
            )

        logs = query.order_by(CallLog.created_at.desc()).all()

        results = []
        for log in logs:
            disk_path = self.resolve_recording_disk_path(log)
            file_size = 0
            computed_dur_str = log.duration or "00:00"
            rec_status = "Not Recorded"

            if log.status == "in-progress" or log.recording_status == "RECORDING":
                rec_status = "Recording..."
            elif disk_path:
                rec_status = "Available"
                file_size = os.path.getsize(disk_path)
                # Ensure accurate duration string from WAV if currently zero
                if computed_dur_str in ("00:00", "", None):
                    try:
                        with wave.open(disk_path, "rb") as wf:
                            rate = wf.getframerate()
                            nframes = wf.getnframes()
                            if rate > 0:
                                total_sec = int(nframes / float(rate))
                                m, s = divmod(total_sec, 60)
                                computed_dur_str = f"{m:02d}:{s:02d}"
                    except Exception:
                        pass
            elif log.recording_status == "FAILED" or log.status == "failed" or log.recording_error:
                rec_status = "Failed"

            # Check if appointment was created during this call
            appt = self.db.query(Appointment).filter(
                Appointment.phone == log.caller_phone,
                Appointment.created_at >= log.created_at - timedelta(minutes=5),
                Appointment.created_at <= log.created_at + timedelta(minutes=30)
            ).first()

            call_type_val = log.call_type.value if hasattr(log.call_type, "value") else str(log.call_type or "Inbound")
            audio_url = f"/api/recordings/{log.id}/audio" if disk_path else None
            download_url = f"/api/recordings/{log.id}/download" if disk_path else None

            results.append({
                "id": log.id,
                "call_sid": log.call_sid,
                "organization_id": log.organization_id,
                "caller_name": log.caller_name or "Unknown",
                "caller_phone": log.caller_phone or "Unknown",
                "call_date": log.created_at.strftime("%Y-%m-%d") if log.created_at else "",
                "start_time": log.created_at.strftime("%H:%M:%S") if log.created_at else "",
                "end_time": (log.updated_at or log.created_at).strftime("%H:%M:%S") if log.created_at else "",
                "duration": computed_dur_str,
                "status": log.status or "Completed",
                "call_type": call_type_val,
                "intent": log.intent or "General Query",
                "emotion": log.emotion or "Neutral - 85%",
                "agent": "AI Receptionist",
                "recording_status": rec_status,
                "recording_url": audio_url,
                "download_url": download_url,
                "file_size": file_size,
                "recording_error": log.recording_error,
                "rag_source": log.rag_source,
                "caller_said": log.caller_said,
                "ai_response": log.ai_response,
                "messages": log.messages,
                "appointment_created": f"{appt.service} ({appt.appointment_date} {appt.appointment_time})" if appt else None
            })
        return results

    def get_recording_file(self, recording_id: int, organization_id: Optional[int] = None) -> Tuple[CallLog, str]:
        query = self.db.query(CallLog).filter(CallLog.id == recording_id)
        if organization_id:
            query = query.filter(CallLog.organization_id == organization_id)
        log = query.first()
        if not log:
            raise HTTPException(status_code=404, detail="Call record not found.")

        disk_path = self.resolve_recording_disk_path(log)
        if not disk_path or not os.path.exists(disk_path) or os.path.getsize(disk_path) <= 44:
            raise HTTPException(
                status_code=404,
                detail="Recording audio file is not available on persistent storage."
            )
        return log, disk_path

    def stream_audio(self, recording_id: int, range_header: Optional[str] = None, organization_id: Optional[int] = None):
        """
        Stream audio with full RFC 7233 byte-range support.
        Allows seeking, scrub-bar jumping, and fast playback in browser HTML5 <audio>.
        """
        log, disk_path = self.get_recording_file(recording_id, organization_id)
        file_size = os.path.getsize(disk_path)

        def iter_chunks(path: str, start: int, length: int, chunk_size: int = 65536):
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        if range_header:
            try:
                range_str = range_header.replace("bytes=", "").strip()
                parts = range_str.split("-")
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            except Exception:
                start = 0
                end = file_size - 1

            if start >= file_size or start < 0 or start > end:
                raise HTTPException(
                    status_code=416,
                    detail="Requested Range Not Satisfiable",
                    headers={"Content-Range": f"bytes */{file_size}"}
                )

            end = min(end, file_size - 1)
            content_length = end - start + 1

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Type": "audio/wav",
            }
            return StreamingResponse(
                iter_chunks(disk_path, start, content_length),
                status_code=206,
                headers=headers
            )
        else:
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": "audio/wav",
            }
            return StreamingResponse(
                iter_chunks(disk_path, 0, file_size),
                status_code=200,
                headers=headers
            )

    def download_audio(self, recording_id: int, organization_id: Optional[int] = None):
        """Provide direct attachment download of finalized recording."""
        log, disk_path = self.get_recording_file(recording_id, organization_id)
        call_type_str = log.call_type.value.lower() if hasattr(log.call_type, "value") else str(log.call_type or "call").lower()
        filename = f"recording_{call_type_str}_{log.id}_{log.call_sid or 'call'}.wav"
        return FileResponse(
            disk_path,
            media_type="audio/wav",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Accept-Ranges": "bytes"
            }
        )

    def generate_bulk_zip(
        self,
        organization_id: Optional[int] = None,
        period: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        recording_ids: Optional[List[int]] = None
    ):
        query = self.db.query(CallLog)
        if organization_id:
            query = query.filter(CallLog.organization_id == organization_id)

        if recording_ids:
            query = query.filter(CallLog.id.in_(recording_ids))
        else:
            s_dt, e_dt = self.get_date_range(period or "monthly", start_date, end_date)
            if s_dt and e_dt:
                query = query.filter(CallLog.created_at >= s_dt, CallLog.created_at <= e_dt)

        logs = query.all()

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            metadata_rows = [
                "Call ID,Call SID,Caller Name,Phone,Type,Date,Duration,Status,Intent,Emotion,Recording Status,Recording File Size"
            ]

            for log in logs:
                disk_path = self.resolve_recording_disk_path(log)
                file_size = os.path.getsize(disk_path) if disk_path else 0
                c_type = log.call_type.value if hasattr(log.call_type, "value") else str(log.call_type or "Inbound")
                r_status = "Available" if disk_path else (log.recording_status or "Not Recorded")

                row = f'"{log.id}","{log.call_sid or ""}","{log.caller_name or ""}","{log.caller_phone or ""}","{c_type}","{log.created_at}","{log.duration or ""}","{log.status or ""}","{log.intent or ""}","{log.emotion or ""}","{r_status}","{file_size}"'
                metadata_rows.append(row)

                if disk_path:
                    audio_filename = f"recording_{c_type.lower()}_{log.id}_{log.call_sid or 'call'}.wav"
                    try:
                        with open(disk_path, "rb") as af:
                            zf.writestr(audio_filename, af.read())
                    except Exception:
                        pass

            zf.writestr("metadata.csv", "\n".join(metadata_rows))

        zip_buffer.seek(0)
        return zip_buffer
