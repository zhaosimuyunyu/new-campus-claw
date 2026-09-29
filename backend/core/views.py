import mimetypes
import secrets
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.contrib.auth import authenticate, login, logout, password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.core.exceptions import ValidationError
from asgiref.sync import async_to_sync
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from .models import Assignment, Course, EmailCode, Material, Membership, StoredFile, Submission, SubmissionVersion, TeacherInvite, User, token_code
from .agent_runtime import index_material

EXTENSIONS = {"doc", "docx", "pdf", "zip", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "png", "jpg", "jpeg", "webp"}
MAX_BYTES = 50 * 1024 * 1024


class CodeThrottle(AnonRateThrottle):
    scope = "code"
    rate = "10/hour"


def fail(message, code=400):
    return Response({"error": message}, status=code)


def member(user, course_id, role=None):
    item = Membership.objects.filter(course_id=course_id, user=user).first()
    return item if item and (role is None or item.role == role) else None


def course_data(course, user):
    m = member(user, course.id)
    return {"id": course.id, "name": course.name, "icon": f"/api/courses/{course.id}/icon/" if course.icon else None,
            "role": m.role, "join_code": course.join_code if m.role == "teacher" else None,
            "member_count": course.memberships.count()}


def file_data(f):
    return {"id": f.id, "name": f.original_name, "size": f.size, "created_at": f.created_at,
            "download_url": f"/api/files/{f.id}/download/"}


def assignment_data(a):
    return {"id": a.id, "title": a.title, "description": a.description, "due_at": a.due_at,
            "allow_late": a.allow_late, "allowed_extensions": a.allowed_extensions,
            "max_file_size_mb": a.max_file_size_mb, "attachment": file_data(a.attachment) if a.attachment else None,
            "created_at": a.created_at}


def validate_file(upload, allowed=EXTENSIONS, max_bytes=MAX_BYTES):
    if not upload:
        return "请选择文件"
    ext = Path(upload.name).suffix.lower().lstrip(".")
    if ext not in allowed:
        return "不支持此文件格式"
    if upload.size == 0 or upload.size > max_bytes:
        return f"文件大小须在 1 字节至 {max_bytes // 1024 // 1024} MB 之间"
    return None


def store_file(course, user, upload, kind):
    return StoredFile.objects.create(course=course, uploaded_by=user, file=upload,
                                     original_name=Path(upload.name).name[:255], size=upload.size, kind=kind)


@ensure_csrf_cookie
@api_view(["GET"])
@permission_classes([AllowAny])
def csrf(request):
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([CodeThrottle])
@csrf_protect
def send_code(request):
    email = str(request.data.get("email", "")).strip().lower()
    purpose = request.data.get("purpose")
    try:
        validate_email(email)
    except ValidationError:
        return fail("邮箱或用途无效")
    if purpose not in ("register", "reset"):
        return fail("邮箱或用途无效")
    exists = User.objects.filter(email=email).exists()
    if (purpose == "register" and exists) or (purpose == "reset" and not exists):
        return fail("邮箱不可用于此操作")
    recent = EmailCode.objects.filter(email=email, purpose=purpose).order_by("-created_at").first()
    if recent and recent.created_at > timezone.now() - timedelta(seconds=60):
        return fail("请稍后再获取验证码", 429)
    code = f"{secrets.randbelow(1000000):06d}"
    send_mail("CampusClaw 验证码", f"验证码：{code}。10 分钟内有效，请勿转发。", settings.DEFAULT_FROM_EMAIL, [email])
    with transaction.atomic():
        EmailCode.objects.filter(email=email, purpose=purpose, consumed_at__isnull=True).update(consumed_at=timezone.now())
        EmailCode.objects.create(email=email, purpose=purpose, code_hash=make_password(code),
                                 expires_at=timezone.now() + timedelta(minutes=10))
    return Response({"ok": True})


def consume_code(email, purpose, code):
    with transaction.atomic():
        item = EmailCode.objects.select_for_update().filter(email=email, purpose=purpose).order_by("-created_at").first()
        if not item or item.consumed_at or item.expires_at <= timezone.now() or item.attempts >= 5:
            return False
        item.attempts += 1
        valid = check_password(code, item.code_hash)
        if valid:
            item.consumed_at = timezone.now()
        item.save(update_fields=["attempts", "consumed_at"])
        return valid


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def register(request):
    email = str(request.data.get("email", "")).strip().lower()
    name = str(request.data.get("display_name", "")).strip()
    password = str(request.data.get("password", ""))
    if User.objects.filter(email=email).exists() or not name or len(name) > 80:
        return fail("请检查邮箱和姓名")
    try:
        password_validation.validate_password(password)
    except ValidationError as error:
        return fail("；".join(error.messages))
    with transaction.atomic():
        if not consume_code(email, "register", str(request.data.get("code", ""))):
            return fail("验证码无效或已过期")
        user = User.objects.create_user(email=email, password=password, display_name=name)
    login(request, user)
    return Response({"id": user.id, "email": user.email, "display_name": name}, status=201)


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def sign_in(request):
    user = authenticate(request, email=str(request.data.get("email", "")).strip().lower(), password=request.data.get("password"))
    if not user:
        return fail("邮箱或密码错误", 400)
    login(request, user)
    return Response({"ok": True})


@api_view(["POST"])
def sign_out(request):
    logout(request)
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def reset_password(request):
    email = str(request.data.get("email", "")).strip().lower()
    password = str(request.data.get("password", ""))
    if not User.objects.filter(email=email).exists():
        return fail("邮箱或密码无效")
    user = User.objects.get(email=email)
    try:
        password_validation.validate_password(password, user)
    except ValidationError as error:
        return fail("；".join(error.messages))
    with transaction.atomic():
        if not consume_code(email, "reset", str(request.data.get("code", ""))):
            return fail("验证码无效或已过期")
        user.set_password(password)
        user.save(update_fields=["password"])
    return Response({"ok": True})


@api_view(["GET"])
def me(request):
    u = request.user
    return Response({"id": u.id, "email": u.email, "display_name": u.display_name,
                     "can_create_course": u.can_create_course})


@api_view(["POST"])
def redeem_invite(request):
    raw = str(request.data.get("code", "")).strip()
    hashed = salted_hmac("teacher-invite", raw).hexdigest()
    with transaction.atomic():
        invite = TeacherInvite.objects.select_for_update().filter(code_hash=hashed).first()
        if not invite or invite.used_at or invite.expires_at <= timezone.now():
            return fail("邀请码无效或已过期")
        invite.used_by = request.user
        invite.used_at = timezone.now()
        invite.save(update_fields=["used_by", "used_at"])
        request.user.can_create_course = True
        request.user.save(update_fields=["can_create_course"])
    return Response({"ok": True})


@api_view(["GET", "POST"])
def courses(request):
    if request.method == "GET":
        qs = Course.objects.filter(memberships__user=request.user).distinct().order_by("-created_at")
        return Response([course_data(c, request.user) for c in qs])
    if not request.user.can_create_course:
        return fail("需要教师邀请码", 403)
    name = str(request.data.get("name", "")).strip()
    if not name or len(name) > 120:
        return fail("课程名不能为空且最多 120 字")
    icon = request.FILES.get("icon")
    if icon and (icon.size > 2 * 1024 * 1024 or Path(icon.name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")):
        return fail("课程图标仅支持 2 MB 内的图片")
    with transaction.atomic():
        course = Course.objects.create(name=name, icon=icon or "", created_by=request.user)
        Membership.objects.create(course=course, user=request.user, role="teacher")
    return Response(course_data(course, request.user), status=201)


@api_view(["POST"])
def join_course(request):
    code = str(request.data.get("code", "")).strip().upper()
    course = Course.objects.filter(join_code=code).first()
    if not course:
        return fail("课程码无效")
    if member(request.user, course.id):
        return fail("你已加入这门课程")
    Membership.objects.create(course=course, user=request.user, role="student")
    return Response(course_data(course, request.user), status=201)


@api_view(["GET"])
def course_detail(request, course_id):
    if not member(request.user, course_id):
        return fail("无权访问课程", 403)
    return Response(course_data(get_object_or_404(Course, pk=course_id), request.user))


@api_view(["GET"])
def course_icon(request, course_id):
    if not member(request.user, course_id):
        return fail("无权查看课程", 403)
    course = get_object_or_404(Course, pk=course_id)
    if not course.icon or not course.icon.storage.exists(course.icon.name):
        return fail("图标不存在", 404)
    response = FileResponse(course.icon.open("rb"))
    response["Content-Type"] = mimetypes.guess_type(course.icon.name)[0] or "application/octet-stream"
    return response


@api_view(["POST"])
def reset_join_code(request, course_id):
    if not member(request.user, course_id, "teacher"):
        return fail("仅教师可重置课程码", 403)
    course = get_object_or_404(Course, pk=course_id)
    course.join_code = token_code()
    course.save(update_fields=["join_code"])
    return Response({"join_code": course.join_code})


@api_view(["GET"])
def members(request, course_id):
    if not member(request.user, course_id):
        return fail("无权查看成员", 403)
    rows = Membership.objects.filter(course_id=course_id).select_related("user").order_by("joined_at")
    return Response([{"id": m.user_id, "name": m.user.display_name, "email": m.user.email, "role": m.role} for m in rows])


@api_view(["GET", "POST"])
def materials(request, course_id):
    m = member(request.user, course_id)
    if not m:
        return fail("无权访问课程", 403)
    course = get_object_or_404(Course, pk=course_id)
    if request.method == "GET":
        return Response([{"id": x.id, "title": x.title, "attachment": file_data(x.attachment), "created_at": x.created_at,
                          "index_status": getattr(getattr(x, "knowledge_entry", None), "status", "pending"),
                          "index_error": getattr(getattr(x, "knowledge_entry", None), "error_message", "")}
                         for x in course.materials.select_related("attachment", "knowledge_entry").order_by("-created_at")])
    if m.role != "teacher":
        return fail("仅教师可上传资料", 403)
    upload = request.FILES.get("file")
    error = validate_file(upload)
    if error:
        return fail(error)
    f = store_file(course, request.user, upload, "material")
    item = Material.objects.create(course=course, title=str(request.data.get("title", "")).strip()[:200] or f.original_name, attachment=f)
    try:
        index_material(item)
    except Exception:
        pass
    entry = getattr(item, "knowledge_entry", None)
    return Response({"id": item.id, "title": item.title, "attachment": file_data(f),
                     "index_status": entry.status if entry else "failed", "index_error": entry.error_message if entry else ""}, status=201)


@api_view(["GET", "POST"])
def assignments(request, course_id):
    m = member(request.user, course_id)
    if not m:
        return fail("无权访问课程", 403)
    course = get_object_or_404(Course, pk=course_id)
    if request.method == "GET":
        return Response([assignment_data(a) for a in course.assignments.select_related("attachment").order_by("-created_at")])
    if m.role != "teacher":
        return fail("仅教师可发布作业", 403)
    title = str(request.data.get("title", "")).strip()
    from django.utils.dateparse import parse_datetime
    due = parse_datetime(str(request.data.get("due_at", "")))
    if not title or not due or timezone.is_naive(due):
        return fail("请填写标题和带时区的截止时间")
    raw_exts = str(request.data.get("allowed_extensions", "")).strip().lower()
    exts = [e.strip().lstrip(".") for e in raw_exts.split(",") if e.strip()] if raw_exts else []
    if any(e not in EXTENSIONS for e in exts):
        return fail("包含不支持的文件格式")
    try:
        max_mb = int(request.data.get("max_file_size_mb", 50))
    except (TypeError, ValueError):
        return fail("文件大小限制无效")
    if not 1 <= max_mb <= 50:
        return fail("文件大小限制须为 1-50 MB")
    upload = request.FILES.get("file")
    if upload:
        error = validate_file(upload)
        if error:
            return fail(error)
    f = store_file(course, request.user, upload, "assignment") if upload else None
    item = Assignment.objects.create(course=course, title=title[:200], description=str(request.data.get("description", "")),
        due_at=due, allow_late=str(request.data.get("allow_late", "false")).lower() == "true",
        allowed_extensions=exts, max_file_size_mb=max_mb, attachment=f)
    return Response(assignment_data(item), status=201)


@api_view(["GET"])
def assignment_detail(request, assignment_id):
    a = get_object_or_404(Assignment.objects.select_related("attachment"), pk=assignment_id)
    if not member(request.user, a.course_id):
        return fail("无权访问作业", 403)
    data = assignment_data(a)
    if member(request.user, a.course_id, "student"):
        s = Submission.objects.filter(assignment=a, student=request.user).first()
        data["my_versions"] = [{"number": v.number, "submitted_at": v.submitted_at, "is_late": v.is_late,
                                "attachment": file_data(v.attachment)} for v in s.versions.select_related("attachment").order_by("-number")] if s else []
    return Response(data)


@api_view(["GET"])
def submissions(request, assignment_id):
    a = get_object_or_404(Assignment, pk=assignment_id)
    if not member(request.user, a.course_id, "teacher"):
        return fail("仅教师可查看提交", 403)
    rows = Submission.objects.filter(assignment=a).select_related("student").prefetch_related("versions__attachment")
    return Response([{"student": {"id": s.student_id, "name": s.student.display_name, "email": s.student.email},
                      "versions": [{"number": v.number, "submitted_at": v.submitted_at, "is_late": v.is_late,
                                    "attachment": file_data(v.attachment)} for v in s.versions.all().order_by("-number")]}
                     for s in rows])


@api_view(["POST"])
def submit(request, assignment_id):
    a = get_object_or_404(Assignment, pk=assignment_id)
    if not member(request.user, a.course_id, "student"):
        return fail("仅本课程学生可提交", 403)
    late = timezone.now() > a.due_at
    if late and not a.allow_late:
        return fail("作业已截止", 403)
    upload = request.FILES.get("file")
    error = validate_file(upload, set(a.allowed_extensions) if a.allowed_extensions else EXTENSIONS, a.max_file_size_mb * 1024 * 1024)
    if error:
        return fail(error)
    with transaction.atomic():
        submission, _ = Submission.objects.select_for_update().get_or_create(assignment=a, student=request.user)
        next_number = (submission.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
        f = store_file(a.course, request.user, upload, "submission")
        version = SubmissionVersion.objects.create(submission=submission, attachment=f, number=next_number, is_late=late)
    return Response({"number": version.number, "is_late": late, "attachment": file_data(f)}, status=201)


@api_view(["GET"])
def download_file(request, file_id):
    f = get_object_or_404(StoredFile, pk=file_id)
    m = member(request.user, f.course_id)
    if not m:
        return fail("无权下载", 403)
    if f.kind == "material":
        allowed = Material.objects.filter(course_id=f.course_id, attachment=f).exists()
    elif f.kind == "assignment":
        allowed = Assignment.objects.filter(course_id=f.course_id, attachment=f).exists()
    else:
        versions = SubmissionVersion.objects.filter(attachment=f, submission__assignment__course_id=f.course_id)
        allowed = versions.filter(submission__student=request.user).exists() if m.role == "student" else versions.exists()
    if not allowed:
        return fail("无权下载", 403)
    if not f.file.storage.exists(f.file.name):
        return fail("文件不存在", 404)
    response = FileResponse(f.file.open("rb"), as_attachment=True, filename=f.original_name)
    response["Content-Type"] = mimetypes.guess_type(f.original_name)[0] or "application/octet-stream"
    return response
