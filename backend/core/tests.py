from datetime import timedelta
import asyncio
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.test import APIClient
from .models import (AgentArtifact, AgentMessage, AgentThread, Assignment, Course, EmailCode,
                    Membership, TeacherInvite, User)
from .agent_runtime import ToolContext, ToolRegistry, ToolSpec


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WorkflowTests(TestCase):
    def setUp(self):
        media_dir = TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.teacher = User.objects.create_user("teacher@example.com", "secure-pass-123", display_name="Teacher", can_create_course=True)
        self.student = User.objects.create_user("student@example.com", "secure-pass-123", display_name="Student")
        self.other = User.objects.create_user("other@example.com", "secure-pass-123", display_name="Other")
        self.tc = APIClient()
        self.sc = APIClient()
        self.oc = APIClient()
        self.tc.force_login(self.teacher)
        self.sc.force_login(self.student)
        self.oc.force_login(self.other)

    def upload(self, name="answer.docx"):
        return SimpleUploadedFile(name, b"course work", content_type="application/octet-stream")

    def download_status(self, client, file_id):
        response = client.get(f"/api/files/{file_id}/download/")
        result = response.status_code
        if getattr(response, "file_to_stream", None) is not None:
            response.file_to_stream.close()
        return result

    def test_registration_code_one_time_and_expiry(self):
        client = APIClient()
        self.assertEqual(client.post("/api/auth/code/", {"email": "new@example.com", "purpose": "register"}).status_code, 200)
        code = mail.outbox[-1].body.split("验证码：")[1][:6]
        data = {"email": "new@example.com", "display_name": "New", "password": "secure-pass-123", "code": code}
        self.assertEqual(client.post("/api/auth/register/", data).status_code, 201)
        self.assertEqual(client.post("/api/auth/register/", data).status_code, 400)
        client.post("/api/auth/code/", {"email": "expired@example.com", "purpose": "register"})
        old = EmailCode.objects.get(email="expired@example.com")
        old.expires_at = timezone.now() - timedelta(seconds=1)
        old.save()
        expired_code = mail.outbox[-1].body.split("验证码：")[1][:6]
        self.assertEqual(client.post("/api/auth/register/", {**data, "email": "expired@example.com", "code": expired_code}).status_code, 400)

    def test_anonymous_auth_writes_require_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        self.assertEqual(client.post("/api/auth/code/", {"email": "new@example.com", "purpose": "register"}).status_code, 403)
        client.get("/api/csrf/")
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/api/auth/code/", {"email": "new@example.com", "purpose": "register"}, HTTP_X_CSRFTOKEN=token).status_code, 200)

    def test_invite_create_join_and_reset_code(self):
        raw = "secret-teacher-invite"
        TeacherInvite.objects.create(code_hash=salted_hmac("teacher-invite", raw).hexdigest(), expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.sc.post("/api/courses/", {"name": "Forbidden"}).status_code, 403)
        self.assertEqual(self.sc.post("/api/teacher-invite/redeem/", {"code": raw}).status_code, 200)
        self.assertEqual(self.oc.post("/api/teacher-invite/redeem/", {"code": raw}).status_code, 400)
        created = self.sc.post("/api/courses/", {"name": "Another course"})
        self.assertEqual(created.status_code, 201)
        course_id = created.data["id"]
        old_code = created.data["join_code"]
        self.assertEqual(self.oc.post("/api/courses/join/", {"code": old_code}).status_code, 201)
        self.assertEqual(self.oc.post("/api/courses/join/", {"code": old_code}).status_code, 400)
        self.assertEqual(self.oc.post(f"/api/courses/{course_id}/code/reset/").status_code, 403)
        new_code = self.sc.post(f"/api/courses/{course_id}/code/reset/").data["join_code"]
        self.assertNotEqual(old_code, new_code)
        self.assertEqual(self.tc.post("/api/courses/join/", {"code": old_code}).status_code, 400)

    def test_files_submission_versions_and_permissions(self):
        created = self.tc.post("/api/courses/", {"name": "Software"})
        course_id = created.data["id"]
        self.sc.post("/api/courses/join/", {"code": created.data["join_code"]})
        material = self.tc.post(f"/api/courses/{course_id}/materials/", {"file": self.upload("notes.pdf")}, format="multipart")
        self.assertEqual(material.status_code, 201)
        file_id = material.data["attachment"]["id"]
        self.assertEqual(self.download_status(self.sc, file_id), 200)
        self.assertEqual(self.download_status(self.oc, file_id), 403)
        self.assertEqual(self.sc.post(f"/api/courses/{course_id}/materials/", {"file": self.upload()}, format="multipart").status_code, 403)
        due = (timezone.now() + timedelta(days=1)).isoformat()
        assignment = self.tc.post(f"/api/courses/{course_id}/assignments/", {"title": "Essay", "due_at": due, "allowed_extensions": "doc,docx,pdf,zip"})
        self.assertEqual(assignment.status_code, 201)
        aid = assignment.data["id"]
        self.assertEqual(self.sc.post(f"/api/assignments/{aid}/submit/", {"file": self.upload("bad.exe")}, format="multipart").status_code, 400)
        first = self.sc.post(f"/api/assignments/{aid}/submit/", {"file": self.upload()}, format="multipart")
        second = self.sc.post(f"/api/assignments/{aid}/submit/", {"file": self.upload("revised.pdf")}, format="multipart")
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual((first.data["number"], second.data["number"]), (1, 2))
        self.assertEqual(len(self.sc.get(f"/api/assignments/{aid}/").data["my_versions"]), 2)
        self.assertEqual(self.download_status(self.oc, second.data['attachment']['id']), 403)
        self.assertEqual(self.download_status(self.tc, second.data['attachment']['id']), 200)
        same_course_student = User.objects.create_user("classmate@example.com", "secure-pass-123", display_name="Classmate")
        Membership.objects.create(course_id=course_id, user=same_course_student, role="student")
        classmate_client = APIClient()
        classmate_client.force_login(same_course_student)
        self.assertEqual(self.download_status(classmate_client, second.data['attachment']['id']), 403)
        self.assertEqual(self.oc.get(f"/api/assignments/{aid}/submissions/").status_code, 403)
        self.assertEqual(self.tc.get(f"/api/assignments/{aid}/submissions/").status_code, 200)
        self.assertEqual(self.sc.post(f"/api/assignments/{aid}/submit/", {"file": SimpleUploadedFile("big.pdf", b"x" * (1024 * 1024 + 1))}, format="multipart").status_code, 201)
        Assignment.objects.filter(pk=aid).update(due_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.sc.post(f"/api/assignments/{aid}/submit/", {"file": self.upload()}, format="multipart").status_code, 403)
        Assignment.objects.filter(pk=aid).update(allow_late=True, max_file_size_mb=1)
        self.assertEqual(self.sc.post(f"/api/assignments/{aid}/submit/", {"file": SimpleUploadedFile("too-big.pdf", b"x" * (1024 * 1024 + 1))}, format="multipart").status_code, 400)
        late = self.sc.post(f"/api/assignments/{aid}/submit/", {"file": self.upload()}, format="multipart")
        self.assertEqual(late.status_code, 201)
        self.assertTrue(late.data["is_late"])
        self.assertEqual(self.download_status(self.tc, late.data['attachment']['id']), 200)

    def test_cross_course_role(self):
        course = Course.objects.create(name="Outside", created_by=self.teacher)
        Membership.objects.create(course=course, user=self.teacher, role="teacher")
        Membership.objects.create(course=course, user=self.student, role="student")
        second = Course.objects.create(name="Second", created_by=self.student)
        Membership.objects.create(course=second, user=self.student, role="teacher")
        self.assertEqual(self.sc.post(f"/api/courses/{course.id}/assignments/", {"title": "No"}).status_code, 403)
        self.assertEqual(self.sc.get(f"/api/courses/{second.id}/members/").status_code, 200)
        self.assertEqual(self.oc.get(f"/api/courses/{second.id}/members/").status_code, 403)


class AgentRuntimeTests(TransactionTestCase):
    def setUp(self):
        media_dir = TemporaryDirectory()
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.owner = User.objects.create_user("agent@example.com", "secure-pass-123", display_name="Agent")
        self.other = User.objects.create_user("other-agent@example.com", "secure-pass-123", display_name="Other")
        self.course = Course.objects.create(name="Agent course", created_by=self.owner)
        Membership.objects.create(course=self.course, user=self.owner, role="teacher")
        self.thread = AgentThread.objects.create(owner=self.owner, course=self.course)
        self.client = APIClient()
        self.client.force_login(self.owner)
        self.other_client = APIClient()
        self.other_client.force_login(self.other)

    def stream_body(self, response):
        async def collect(stream):
            return b"".join([chunk async for chunk in stream])
        if response.is_async:
            return asyncio.run(collect(response.streaming_content)).decode("utf-8")
        return b"".join(response.streaming_content).decode("utf-8")

    def test_agent_endpoints_require_authentication(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.post("/api/agent/threads/", {"course_id": self.course.id}, format="json").status_code, 401)
        self.assertEqual(anonymous.post("/api/agent/attachments/").status_code, 401)
        self.assertEqual(anonymous.get("/api/agent/tools/").status_code, 401)

    def test_thread_artifact_and_course_scope_are_isolated(self):
        self.assertEqual(self.other_client.get(f"/api/agent/threads/{self.thread.id}/messages/").status_code, 404)
        self.assertEqual(self.other_client.post("/api/agent/threads/", {"course_id": self.course.id}, format="json").status_code, 404)
        artifact = AgentArtifact.objects.create(owner=self.owner, thread=self.thread, course=self.course,
            name="private.pdf", mime_type="application/pdf", preview_html="private")
        self.assertEqual(self.other_client.get(f"/api/agent/artifacts/{artifact.id}/preview/").status_code, 404)

    def test_rag_disabled_streams_and_persists_without_search(self):
        with patch("core.agent_views.search", side_effect=AssertionError("RAG must stay disabled")) as search_mock:
            response = self.client.post(f"/api/agent/threads/{self.thread.id}/messages/stream/",
                {"content": "普通对话", "use_course_materials": False, "attachments": []}, format="json")
            body = self.stream_body(response)
        self.assertIn("event: message_start", body)
        self.assertIn("event: delta", body)
        self.assertIn("event: message_end", body)
        search_mock.assert_not_called()
        self.assertEqual(list(self.thread.messages.values_list("role", flat=True)), ["user", "assistant"])

    def test_rag_no_hit_does_not_call_model(self):
        with patch("core.agent_views.search", new=AsyncMock(return_value=[])), patch("core.agent_views.model_stream") as model:
            response = self.client.post(f"/api/agent/threads/{self.thread.id}/messages/stream/",
                {"content": "找一个不存在的概念", "use_course_materials": True}, format="json")
            body = self.stream_body(response)
        self.assertIn("资料中未找到相关内容", body)
        self.assertNotIn("event: error", body)
        model.assert_not_called()
        self.assertEqual(AgentMessage.objects.filter(thread=self.thread, role="assistant").get().content, "资料中未找到相关内容")

    def test_tool_registry_validates_permissions_and_schema(self):
        registry = ToolRegistry()
        registry.register(ToolSpec("echo", "1.0", {"type": "object", "required": ["value"]}, {"type": "string"}, "test.read", False, False),
                         lambda arguments, context: arguments["value"])
        context = ToolContext(self.owner, self.thread, frozenset({"test.read"}))
        self.assertEqual(asyncio.run(registry.execute("echo", {"value": "ok"}, context)), "ok")
        with self.assertRaises(ValueError):
            asyncio.run(registry.execute("echo", {}, context))
        with self.assertRaises(PermissionError):
            asyncio.run(registry.execute("echo", {"value": "ok"}, ToolContext(self.owner, self.thread)))
