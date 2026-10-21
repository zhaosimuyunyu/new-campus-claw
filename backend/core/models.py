import secrets
from pathlib import Path
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from pgvector.django import VectorField


def token_code(length=8):
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(length))


def upload_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f"courses/{instance.course_id}/{secrets.token_hex(16)}{suffix}"


def artifact_upload_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f"agent-artifacts/{instance.owner_id}/{secrets.token_hex(16)}{suffix}"


def agent_attachment_upload_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f"agent-attachments/{instance.owner_id}/{secrets.token_hex(16)}{suffix}"


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, is_active=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=80)
    can_create_course = models.BooleanField(default=False)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()


class EmailCode(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "register", "Register"
        RESET = "reset", "Reset password"
    email = models.EmailField()
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["email", "purpose", "-created_at"])]


class TeacherInvite(models.Model):
    code_hash = models.CharField(max_length=128, unique=True)
    label = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField()
    used_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Course(models.Model):
    name = models.CharField(max_length=120)
    icon = models.FileField(upload_to="course-icons/", blank=True)
    join_code = models.CharField(max_length=12, unique=True, default=token_code)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Membership(models.Model):
    class Role(models.TextChoices):
        TEACHER = "teacher", "Teacher"
        STUDENT = "student", "Student"
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=8, choices=Role.choices)
    joined_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["course", "user"], name="unique_course_user")]


class StoredFile(models.Model):
    class Kind(models.TextChoices):
        MATERIAL = "material", "Material"
        ASSIGNMENT = "assignment", "Assignment attachment"
        SUBMISSION = "submission", "Submission"
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    file = models.FileField(upload_to=upload_path)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=12, choices=Kind.choices)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Material(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="materials")
    title = models.CharField(max_length=200)
    attachment = models.OneToOneField(StoredFile, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class KnowledgeEntry(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    material = models.OneToOneField(Material, on_delete=models.CASCADE, related_name="knowledge_entry")
    body_text = models.TextField(blank=True)
    parser = models.CharField(max_length=32, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    chunk_strategy = models.CharField(max_length=16, default="auto")
    indexed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class KnowledgeChunk(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    entry = models.ForeignKey(KnowledgeEntry, on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.PositiveIntegerField()
    chunk_text = models.TextField()
    start_offset = models.PositiveIntegerField(default=0)
    end_offset = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["entry", "chunk_index"], name="unique_knowledge_chunk")]


class ChunkEmbedding(models.Model):
    chunk = models.OneToOneField(KnowledgeChunk, on_delete=models.CASCADE, related_name="embedding_record")
    embedding = VectorField(dimensions=2048, null=True, blank=True)
    model = models.CharField(max_length=160, blank=True)
    dimensions = models.PositiveIntegerField(default=2048)
    created_at = models.DateTimeField(auto_now_add=True)


class Assignment(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="assignments")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_at = models.DateTimeField()
    allow_late = models.BooleanField(default=False)
    allowed_extensions = models.JSONField(default=list, blank=True)
    max_file_size_mb = models.PositiveSmallIntegerField(default=50)
    attachment = models.OneToOneField(StoredFile, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentThread(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_threads")
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.CASCADE, related_name="agent_threads")
    title = models.CharField(max_length=200, default="新对话")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class UserSkill(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500)
    instructions = models.TextField()
    resources = models.JSONField(default=dict)
    tool_refs = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class AssistantConfig(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    app_id = models.CharField(max_length=60, default="general")
    template = models.TextField(blank=True)
    skill_ids = models.JSONField(default=list)
    mcp_refs = models.JSONField(default=list)
    use_knowledge = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "course"], name="unique_assistant_config")]


class MCPServer(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.SlugField(max_length=60)
    url = models.URLField()
    token_env = models.CharField(max_length=100, blank=True)
    connected = models.BooleanField(default=False)
    tools = models.JSONField(default=list)
    is_shared = models.BooleanField(default=False)
    session_id = models.CharField(max_length=200, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="unique_mcp_owner_name")]


class AgentMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"

    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12, choices=Role.choices)
    content = models.TextField(blank=True)
    citations = models.JSONField(default=list, blank=True)
    attachments = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentAttachment(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_attachments")
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.CASCADE)
    file = models.FileField(upload_to=agent_attachment_upload_path)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="runs")
    model = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class AgentEvent(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=32)
    payload = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "sequence"], name="unique_agent_event_sequence")]


class AgentArtifact(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_artifacts")
    thread = models.ForeignKey(AgentThread, on_delete=models.CASCADE, related_name="artifacts")
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=160)
    file = models.FileField(upload_to=artifact_upload_path)
    preview_html = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Submission(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["assignment", "student"], name="unique_assignment_student")]


class SubmissionVersion(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="versions")
    attachment = models.OneToOneField(StoredFile, on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    submitted_at = models.DateTimeField(default=timezone.now)
    is_late = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "number"], name="unique_submission_version")]
