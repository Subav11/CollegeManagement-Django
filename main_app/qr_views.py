"""
QR Code-based Attendance System views.
Staff endpoints: generate, refresh, and monitor QR attendance sessions.
Student endpoints: scan QR and validate attendance.
"""
import io
import json
import math
import base64
from datetime import timedelta, date

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
GEO_FENCE_RADIUS_METERS = 50    # Maximum allowed distance from teacher


# ======================================================================
#  GEOLOCATION HELPERS
# ======================================================================

def _haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points
    on Earth using the Haversine formula.
    Returns distance in meters.
    """
    R = 6_371_000  # Earth's radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) *
         math.sin(delta_lambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def _parse_float(value):
    """Safely parse a float from request data, return None on failure."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


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
        'today': date.today().isoformat(),
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
    attendance_date_str = request.POST.get('attendance_date', '')
    staff_lat = _parse_float(request.POST.get('latitude'))
    staff_lon = _parse_float(request.POST.get('longitude'))

    if not subject_id or not session_id:
        return JsonResponse({'error': 'Subject and session are required'}, status=400)

    # Parse attendance date (default to today)
    try:
        attendance_date = date.fromisoformat(attendance_date_str) if attendance_date_str else date.today()
    except ValueError:
        attendance_date = date.today()

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
        attendance_date=attendance_date,
        expires_at=expires_at,
        is_active=True,
        latitude=staff_lat,
        longitude=staff_lon,
    )

    # Generate signed JWT
    payload = {
        'qr_session_id': qr_session.pk,
        'subject_id': subject.pk,
        'session_id': session.pk,
        'staff_id': staff.pk,
        'attendance_date': attendance_date.isoformat(),
        'iat': int(now.timestamp()),
        'exp': int(expires_at.timestamp()),
    }
    # Include geolocation in JWT if available
    if staff_lat is not None and staff_lon is not None:
        payload['latitude'] = staff_lat
        payload['longitude'] = staff_lon

    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)
    qr_session.token = token
    qr_session.save(update_fields=['token'])

    # Generate QR code image as base64
    qr_image_b64 = _generate_qr_image(token)

    has_location = staff_lat is not None and staff_lon is not None

    return JsonResponse({
        'success': True,
        'qr_image': qr_image_b64,
        'qr_session_id': qr_session.pk,
        'expires_at': expires_at.isoformat(),
        'expiry_seconds': QR_EXPIRY_MINUTES * 60,
        'subject_name': subject.name,
        'attendance_date': attendance_date.isoformat(),
        'has_location': has_location,
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
        'attendance_date': qr_session.attendance_date.isoformat(),
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
    student_lat = _parse_float(request.POST.get('latitude'))
    student_lon = _parse_float(request.POST.get('longitude'))

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

    # ── Geolocation check (50m radius) ──
    teacher_lat = qr_session.latitude
    teacher_lon = qr_session.longitude

    if teacher_lat is not None and teacher_lon is not None:
        # Teacher had location — enforce geo-fence
        if student_lat is None or student_lon is None:
            return JsonResponse({
                'success': False,
                'error': 'Location access is required to mark attendance. '
                         'Please enable location services and try again.'
            })

        distance = _haversine_distance(teacher_lat, teacher_lon, student_lat, student_lon)
        if distance > GEO_FENCE_RADIUS_METERS:
            return JsonResponse({
                'success': False,
                'error': f'You are approximately {int(distance)}m away from the classroom. '
                         f'You must be within {GEO_FENCE_RADIUS_METERS}m to mark attendance.'
            })

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

    # ── Create or get Attendance record for the attendance_date ──
    att_date = qr_session.attendance_date
    attendance, _ = Attendance.objects.get_or_create(
        subject=subject,
        session=qr_session.session,
        date=att_date,
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
        scan_latitude=student_lat,
        scan_longitude=student_lon,
    )

    return JsonResponse({
        'success': True,
        'status': status,
        'subject': subject.name,
        'attendance_date': att_date.isoformat(),
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
