import secrets
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone
from django.utils.crypto import salted_hmac
from .models import (AgentArtifact, AgentEvent, AgentMessage, AgentRun, AgentThread, ChunkEmbedding, KnowledgeChunk,
                     KnowledgeEntry, User, TeacherInvite, Course, Membership, Material, Assignment, Submission, SubmissionVersion)


@admin.register(User)
class CampusUserAdmin(UserAdmin):
    model = User
    ordering = ("email",)
    list_display = ("email", "display_name", "can_create_course", "is_staff")
    fieldsets = ((None, {"fields": ("email", "password")}), ("Profile", {"fields": ("display_name", "can_create_course")}),
                 ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}))
    add_fieldsets = ((None, {"fields": ("email", "password1", "password2")}),)


@admin.register(TeacherInvite)
class TeacherInviteAdmin(admin.ModelAdmin):
    list_display = ("id", "label", "expires_at", "used_by", "used_at")
    readonly_fields = ("code_hash", "used_by", "used_at", "created_at")
    fields = ("label", "expires_at", "code_hash", "used_by", "used_at", "created_at")
    def save_model(self, request, obj, form, change):
        if not change:
            raw = secrets.token_urlsafe(18)
            obj.code_hash = salted_hmac("teacher-invite", raw).hexdigest()
            super().save_model(request, obj, form, change)
            messages.success(request, f"教师邀请码（仅显示一次，请立即记录）：{raw}")
        else:
            super().save_model(request, obj, form, change)


admin.site.register([Course, Membership, Material, Assignment, Submission, SubmissionVersion, AgentThread, AgentMessage,
                     AgentRun, AgentEvent, AgentArtifact, KnowledgeEntry, KnowledgeChunk, ChunkEmbedding])
