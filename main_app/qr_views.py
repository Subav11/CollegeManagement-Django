"""
QR Code-based Attendance System views.
Staff endpoints: generate, refresh, and monitor QR attendance sessions.
Student endpoints: scan QR and validate attendance.
"""
import io
import json
import base64
from datetime import timedelta

import jwt
import qrcode

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import (
    Attendance, AttendanceReport, QRAttendanceLog, QRAttendanceSession,
    Session, Staff, Student, Subject,
)

# ── Configuration ──────────────────────────────────────────────────────
QR_EXPIRY_MINUTES = 10          # Fixed 10-minute window
LATE_THRESHOLD_PERCENT = 0.80   # Last 20% of window = "late"
JWT_ALGORITHM = "HS256"


# ======================================================================
#  STAFF / TEACHER ENDPOINTS
# ======================================================================

def staff_qr_attendance(request):
    """Render QR attendance generation page."""
    staff = get_object_or_404(Staff, admin=request.user)
    subjects = Subject.objects.filter(staff=staff)
    sessions = Session.objects.all()
    context = {
        'subjects': subjects,
        'sessions': sessions,
        'page_title': 'QR Code Attendance',
        'qr_expiry_minutes': QR_EXPIRY_MINUTES,
    }
    return render(request, 'staff_template/staff_qr_attendance.html', context)


@csrf_exempt
def staff_generate_qr(request):
    """Generate a new QR attendance session and return QR image + metadata."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    staff = get_object_or_404(Staff, admin=request.user)
    subject_id = request.POST.get('subject')
    session_id = request.POST.get('session')

    if not subject_id or not session_id:
        return JsonResponse({'error': 'Subject and session are required'}, status=400)

    try:
        subject = get_object_or_404(Subject, id=subject_id, staff=staff)
        session = get_object_or_404(Session, id=session_id)
    except Exception:
        return JsonResponse({'error': 'Invalid subject or session'}, status=400)

    # Deactivate any previous active QR sessions for this staff+subject
    QRAttendanceSession.objects.filter(
        staff=staff, subject=subject, is_active=True
    ).update(is_active=False)

    now = timezone.now()
    expires_at = now + timedelta(minutes=QR_EXPIRY_MINUTES)

    # Create session record first (need pk for token)
    qr_session = QRAttendanceSession.objects.create(
        staff=staff,
        subject=subject,
        session=session,
        token='placeholder',
        expires_at=expires_at,
        is_active=True,
    )

    # Generate signed JWT
    payload = {
        'qr_session_id': qr_session.pk,
        'subject_id': subject.pk,
        'session_id': session.pk,
        'staff_id': staff.pk,
        'iat': int(now.timestamp()),
        'exp': int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)
    qr_session.token = token
    qr_session.save(update_fields=['token'])

    # Generate QR code image as base64
    qr_image_b64 = _generate_qr_image(token)

    return JsonResponse({
        'success': True,
        'qr_image': qr_image_b64,
        'qr_session_id': qr_session.pk,
        'expires_at': expires_at.isoformat(),
        'expiry_seconds': QR_EXPIRY_MINUTES * 60,
        'subject_name': subject.name,
    })


@csrf_exempt
def staff_refresh_qr(request):
    """Deactivate old QR and generate a new one."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    staff = get_object_or_404(Staff, admin=request.user)
    old_session_id = request.POST.get('qr_session_id')

    # Deactivate old session
    if old_session_id:
        QRAttendanceSession.objects.filter(
            pk=old_session_id, staff=staff
        ).update(is_active=False)

    # Re-use the generate logic
    return staff_generate_qr(request)


@csrf_exempt
def staff_qr_attendance_log(request):
    """Return live scan log for a QR session (polled by staff dashboard)."""
    qr_session_id = request.GET.get('qr_session_id') or request.POST.get('qr_session_id')
    if not qr_session_id:
        return JsonResponse({'logs': []})

    staff = get_object_or_404(Staff, admin=request.user)
    try:
        qr_session = QRAttendanceSession.objects.get(pk=qr_session_id, staff=staff)
    except QRAttendanceSession.DoesNotExist:
        return JsonResponse({'logs': []})

    logs = QRAttendanceLog.objects.filter(qr_session=qr_session).select_related(
        'student', 'student__admin'
    )
    log_data = []
    for log in logs:
        log_data.append({
            'student_name': f"{log.student.admin.first_name} {log.student.admin.last_name}",
            'status': log.status,
            'scanned_at': log.scanned_at.strftime('%I:%M:%S %p'),
        })

    return JsonResponse({
        'logs': log_data,
        'total': len(log_data),
        'is_active': qr_session.is_active and qr_session.expires_at > timezone.now(),
    })


# ======================================================================
#  STUDENT ENDPOINTS
# ======================================================================

def student_scan_qr(request):
    """Render QR scanner page for students."""
    student = get_object_or_404(Student, admin=request.user)
    context = {
        'page_title': 'Scan QR Attendance',
        'student': student,
    }
    return render(request, 'student_template/student_scan_qr.html', context)


@csrf_exempt
def student_validate_qr(request):
    """Validate scanned QR token, mark attendance."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    student = get_object_or_404(Student, admin=request.user)
    token = request.POST.get('token', '').strip()
    user_agent = request.META.get('HTTP_USER_AGENT', '')

    if not token:
        return JsonResponse({'success': False, 'error': 'No QR data received'})

    # ── Verify JWT ──
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return JsonResponse({'success': False, 'error': 'This QR code has expired. Please ask your teacher to generate a new one.'})
    except jwt.InvalidTokenError:
        return JsonResponse({'success': False, 'error': 'Invalid QR code. Please scan a valid attendance QR.'})

    # ── Load QR session ──
    try:
        qr_session = QRAttendanceSession.objects.get(
            pk=payload['qr_session_id'], token=token
        )
    except QRAttendanceSession.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'QR session not found or has been refreshed.'})

    # ── Check active + not expired ──
    now = timezone.now()
    if not qr_session.is_active:
        return JsonResponse({'success': False, 'error': 'This QR session has been deactivated.'})
    if now > qr_session.expires_at:
        return JsonResponse({'success': False, 'error': 'This QR code has expired.'})

    # ── Check student belongs to the subject's course ──
    subject = qr_session.subject
    if student.course_id != subject.course_id:
        return JsonResponse({'success': False, 'error': 'You are not enrolled in this course.'})

    # ── Check duplicate scan ──
    if QRAttendanceLog.objects.filter(qr_session=qr_session, student=student).exists():
        return JsonResponse({'success': False, 'error': 'You have already marked attendance for this session.'})

    # ── Determine status (present vs late) ──
    total_window = (qr_session.expires_at - qr_session.created_at).total_seconds()
    elapsed = (now - qr_session.created_at).total_seconds()
    status = 'late' if elapsed > (total_window * LATE_THRESHOLD_PERCENT) else 'present'

    # ── Create or get Attendance record for today ──
    today = now.date()
    attendance, _ = Attendance.objects.get_or_create(
        subject=subject,
        session=qr_session.session,
        date=today,
    )

    # ── Create AttendanceReport (integrates with existing system) ──
    attendance_report, created = AttendanceReport.objects.get_or_create(
        student=student,
        attendance=attendance,
        defaults={'status': True},
    )
    if not created:
        # Student already has a report for today (maybe from manual attendance)
        attendance_report.status = True
        attendance_report.save()

    # ── Create QR log ──
    QRAttendanceLog.objects.create(
        qr_session=qr_session,
        student=student,
        attendance_report=attendance_report,
        status=status,
        user_agent=user_agent,
    )

    return JsonResponse({
        'success': True,
        'status': status,
        'subject': subject.name,
        'message': f'Attendance marked as {"Present" if status == "present" else "Late"} for {subject.name}',
        'scanned_at': now.strftime('%I:%M:%S %p'),
    })


# ======================================================================
#  HELPERS
# ======================================================================

def _generate_qr_image(data: str) -> str:
    """Generate a QR code PNG and return as base64-encoded data URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="#ffffff")

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{img_b64}"
