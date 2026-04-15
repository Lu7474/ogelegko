from django.urls import path

from . import admin_catalog, admin_students, admin_variants, admin_views, views

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
    path("attempt/<int:attempt_id>/retry/", views.retry_mistakes, name="retry_mistakes"),
    path("profile/", views.profile_view, name="profile"),
    # Админ — вход / дашборд / экспорт
    path("admin/", admin_views.admin_login, name="admin_login"),
    path("admin/logout/", admin_views.admin_logout, name="admin_logout"),
    path("admin/dashboard/", admin_views.dashboard, name="admin_dashboard"),
    path("admin/export/", admin_views.export_results, name="admin_export"),
    path("admin/export/docx/", admin_views.export_results_docx, name="admin_export_docx"),
    # Админ — классы
    path("admin/classes/", admin_students.class_list, name="admin_classes"),
    path("admin/classes/add/", admin_students.class_add, name="admin_class_add"),
    path("admin/classes/<int:class_id>/edit/", admin_students.class_edit, name="admin_class_edit"),
    path("admin/classes/<int:class_id>/delete/", admin_students.class_delete, name="admin_class_delete"),
    path("admin/classes/<int:class_id>/toggle/", admin_students.class_toggle, name="admin_class_toggle"),
    path("admin/classes/<int:class_id>/stats/", admin_students.class_stats, name="admin_class_stats"),
    path(
        "admin/classes/<int:class_id>/bulk-grade/",
        admin_students.bulk_grade_attempts,
        name="admin_bulk_grade",
    ),
    path(
        "admin/classes/<int:class_id>/stats/excel/",
        admin_students.class_stats_excel,
        name="admin_class_stats_excel",
    ),
    # Админ — ученики
    path("admin/students/", admin_students.student_list, name="admin_students"),
    path("admin/students/add/", admin_students.student_add, name="admin_student_add"),
    path("admin/students/import/", admin_students.student_import, name="admin_student_import"),
    path("admin/students/<int:student_id>/edit/", admin_students.student_edit, name="admin_student_edit"),
    path(
        "admin/students/<int:student_id>/delete/", admin_students.student_delete, name="admin_student_delete"
    ),
    path("admin/students/<int:student_id>/stats/", admin_students.student_stats, name="admin_student_stats"),
    # Админ — варианты
    path("admin/variants/", admin_variants.variant_list, name="admin_variants"),
    path("admin/variants/add/", admin_variants.variant_add, name="admin_variant_add"),
    path(
        "admin/variants/from-catalog/", admin_catalog.variant_from_catalog, name="admin_variant_from_catalog"
    ),
    path(
        "admin/variants/auto-generate/",
        admin_catalog.variant_auto_generate,
        name="admin_variant_auto_generate",
    ),
    path("admin/variants/import/", admin_variants.variant_import, name="admin_variant_import"),
    path(
        "admin/variants/import/<str:job_id>/status/",
        admin_variants.variant_import_status,
        name="admin_variant_import_status",
    ),
    path("admin/variants/print-zip/", admin_variants.variants_print_zip, name="admin_variants_print_zip"),
    path(
        "admin/variants/bulk-delete/", admin_variants.variants_bulk_delete, name="admin_variants_bulk_delete"
    ),
    path(
        "admin/variants/bulk-toggle/", admin_variants.variants_bulk_toggle, name="admin_variants_bulk_toggle"
    ),
    path("admin/variants/<int:variant_id>/edit/", admin_variants.variant_edit, name="admin_variant_edit"),
    path(
        "admin/variants/<int:variant_id>/toggle/", admin_variants.variant_toggle, name="admin_variant_toggle"
    ),
    path(
        "admin/variants/<int:variant_id>/duplicate/",
        admin_variants.variant_duplicate,
        name="admin_variant_duplicate",
    ),
    path(
        "admin/variants/<int:variant_id>/delete/", admin_variants.variant_delete, name="admin_variant_delete"
    ),
    path("admin/variants/<int:variant_id>/stats/", admin_variants.variant_stats, name="admin_variant_stats"),
    path(
        "admin/variants/<int:variant_id>/print/<str:mode>/",
        admin_variants.variant_print_docx,
        name="admin_variant_print",
    ),
    # Админ — каталог
    path("admin/catalog/", admin_catalog.catalog_list, name="admin_catalog"),
    path("admin/catalog/add/", admin_catalog.catalog_add, name="admin_catalog_add"),
    path("admin/catalog/bulk-delete/", admin_catalog.catalog_bulk_delete, name="admin_catalog_bulk_delete"),
    path("admin/catalog/import/", admin_catalog.catalog_import, name="admin_catalog_import"),
    path(
        "admin/catalog/import/<str:job_id>/status/",
        admin_catalog.catalog_import_status,
        name="admin_catalog_import_status",
    ),
    path("admin/catalog/import-fipi/", admin_catalog.catalog_fipi_import, name="admin_fipi_import"),
    path("admin/catalog/import-fipi/preview/", admin_catalog.catalog_fipi_preview, name="admin_fipi_preview"),
    path("admin/catalog/import-fipi/start/", admin_catalog.catalog_fipi_start, name="admin_fipi_start"),
    path(
        "admin/catalog/import-fipi/<str:job_id>/status/",
        admin_catalog.catalog_fipi_status,
        name="admin_fipi_import_status",
    ),
    path("admin/catalog/imports/", admin_catalog.catalog_import_list, name="admin_import_list"),
    path(
        "admin/catalog/imports/<int:session_id>/delete/",
        admin_catalog.catalog_import_session_delete,
        name="admin_import_session_delete",
    ),
    path(
        "admin/catalog/unclassified/", admin_catalog.catalog_unclassified, name="admin_catalog_unclassified"
    ),
    path("admin/catalog/<int:task_id>/edit/", admin_catalog.catalog_edit, name="admin_catalog_edit"),
    path("admin/catalog/<int:task_id>/delete/", admin_catalog.catalog_delete, name="admin_catalog_delete"),
    path(
        "admin/catalog/<int:task_id>/assign/",
        admin_catalog.catalog_assign_number,
        name="admin_catalog_assign",
    ),
    # Админ — API
    path("admin/api/catalog/", admin_catalog.api_catalog_tasks, name="admin_api_catalog"),
    path("admin/api/new-attempts/", admin_students.api_new_attempts, name="admin_api_new_attempts"),
    # Админ — попытки
    path("admin/attempts/<int:attempt_id>/", admin_students.attempt_detail, name="admin_attempt_detail"),
    path(
        "admin/attempts/<int:attempt_id>/delete/", admin_students.attempt_delete, name="admin_attempt_delete"
    ),
    path(
        "admin/answers/<int:answer_id>/grade/", admin_students.attempt_grade_answer, name="admin_grade_answer"
    ),
]
