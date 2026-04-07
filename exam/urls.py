from django.urls import path
from . import views, admin_views

urlpatterns = [
    # Ученик
    path("", views.login_view, name="login"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("choose/", views.choose_variant, name="choose_variant"),
    path("exam/<int:variant_id>/", views.start_exam, name="start_exam"),
    path("exam/save-answer/", views.save_answer, name="save_answer"),
    path("exam/finish/<int:attempt_id>/", views.finish_exam, name="finish_exam"),
    path("results/<int:attempt_id>/", views.results_view, name="results"),
    path("attempt/<int:attempt_id>/", views.view_attempt, name="view_attempt"),
    path("profile/", views.profile_view, name="profile"),

    # Админ
    path("admin/", admin_views.admin_login, name="admin_login"),
    path("admin/logout/", admin_views.admin_logout, name="admin_logout"),
    path("admin/dashboard/", admin_views.dashboard, name="admin_dashboard"),
    path("admin/export/", admin_views.export_results, name="admin_export"),

    # Админ — классы
    path("admin/classes/", admin_views.class_list, name="admin_classes"),
    path("admin/classes/add/", admin_views.class_add, name="admin_class_add"),
    path("admin/classes/<int:class_id>/edit/", admin_views.class_edit, name="admin_class_edit"),
    path("admin/classes/<int:class_id>/delete/", admin_views.class_delete, name="admin_class_delete"),
    path("admin/classes/<int:class_id>/toggle/", admin_views.class_toggle, name="admin_class_toggle"),
    path("admin/classes/<int:class_id>/stats/", admin_views.class_stats, name="admin_class_stats"),

    # Админ — ученики
    path("admin/students/", admin_views.student_list, name="admin_students"),
    path("admin/students/add/", admin_views.student_add, name="admin_student_add"),
    path("admin/students/import/", admin_views.student_import, name="admin_student_import"),
    path("admin/students/<int:student_id>/edit/", admin_views.student_edit, name="admin_student_edit"),
    path("admin/students/<int:student_id>/delete/", admin_views.student_delete, name="admin_student_delete"),
    path("admin/students/<int:student_id>/stats/", admin_views.student_stats, name="admin_student_stats"),

    # Админ — варианты
    path("admin/variants/", admin_views.variant_list, name="admin_variants"),
    path("admin/variants/add/", admin_views.variant_add, name="admin_variant_add"),
    path("admin/variants/<int:variant_id>/edit/", admin_views.variant_edit, name="admin_variant_edit"),
    path("admin/variants/<int:variant_id>/toggle/", admin_views.variant_toggle, name="admin_variant_toggle"),
    path("admin/variants/<int:variant_id>/duplicate/", admin_views.variant_duplicate, name="admin_variant_duplicate"),
    path("admin/variants/<int:variant_id>/delete/", admin_views.variant_delete, name="admin_variant_delete"),
    path("admin/variants/<int:variant_id>/stats/", admin_views.variant_stats, name="admin_variant_stats"),
    path("admin/variants/import/", admin_views.variant_import, name="admin_variant_import"),
    path("admin/variants/import/<str:job_id>/status/", admin_views.variant_import_status, name="admin_variant_import_status"),

    # Админ — попытки
    path("admin/attempts/<int:attempt_id>/", admin_views.attempt_detail, name="admin_attempt_detail"),
    path("admin/attempts/<int:attempt_id>/delete/", admin_views.attempt_delete, name="admin_attempt_delete"),
    path("admin/answers/<int:answer_id>/grade/", admin_views.attempt_grade_answer, name="admin_grade_answer"),
]
