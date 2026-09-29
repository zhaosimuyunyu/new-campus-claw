from django.urls import path
from . import views
from . import agent_views

urlpatterns = [
    path("csrf/", views.csrf), path("auth/code/", views.send_code),
    path("auth/register/", views.register), path("auth/login/", views.sign_in),
    path("auth/logout/", views.sign_out), path("auth/reset/", views.reset_password),
    path("me/", views.me), path("teacher-invite/redeem/", views.redeem_invite),
    path("courses/", views.courses), path("courses/join/", views.join_course),
    path("courses/<int:course_id>/", views.course_detail),
    path("courses/<int:course_id>/icon/", views.course_icon),
    path("courses/<int:course_id>/code/reset/", views.reset_join_code),
    path("courses/<int:course_id>/members/", views.members),
    path("courses/<int:course_id>/materials/", views.materials),
    path("courses/<int:course_id>/assignments/", views.assignments),
    path("assignments/<int:assignment_id>/", views.assignment_detail),
    path("assignments/<int:assignment_id>/submissions/", views.submissions),
    path("assignments/<int:assignment_id>/submit/", views.submit),
    path("files/<int:file_id>/download/", views.download_file),
    path("courses/<int:course_id>/knowledge/search/", agent_views.knowledge_search),
    path("courses/<int:course_id>/knowledge/reindex/", agent_views.reindex_course),
    path("materials/<int:material_id>/reindex/", agent_views.reindex_material),
    path("agent/threads/", agent_views.threads),
    path("agent/threads/<int:thread_id>/messages/", agent_views.thread_messages),
    path("agent/threads/<int:thread_id>/messages/stream/", agent_views.agent_stream),
    path("agent/threads/<int:thread_id>/artifacts/", agent_views.create_artifact_view),
    path("agent/attachments/", agent_views.upload_attachment),
    path("agent/runs/<int:run_id>/cancel/", agent_views.cancel_run),
    path("agent/artifacts/<int:artifact_id>/download/", agent_views.artifact_download),
    path("agent/artifacts/<int:artifact_id>/preview/", agent_views.artifact_preview),
    path("agent/tools/", agent_views.tools),
    path("agent/tools/<str:tool_name>/execute/", agent_views.execute_tool),
]
