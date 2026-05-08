import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import Answer, Attempt, ExamType, SchoolClass, Student, Task, Variant


class AdminTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            "admin",
            password="admin123",
            is_staff=True,
        )

    def test_admin_login(self):
        resp = self.client.post(
            "/admin/",
            {
                "username": "admin",
                "password": "admin123",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_admin_logout_requires_post(self):
        self.client.login(username="admin", password="admin123")
        resp = self.client.get("/admin/logout/")
        self.assertEqual(resp.status_code, 405)

    def test_dashboard_requires_auth(self):
        resp = self.client.get("/admin/dashboard/")
        self.assertEqual(resp.status_code, 302)

    def test_export_results(self):
        self.client.login(username="admin", password="admin123")
        resp = self.client.get("/admin/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheet", resp["Content-Type"])


class AdminExportTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user("admin_exp", password="pass", is_staff=True)
        self.client.login(username="admin_exp", password="pass")

        self.school_class = SchoolClass.objects.create(name="9Г", exam_type=ExamType.OGE)
        self.student = Student(full_name="Экспортов Экспорт", school_class=self.school_class)
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(number="export_v1", exam_type=ExamType.OGE)
        self.task = Task.objects.create(
            variant=self.variant, number="1", text="1+1=?", correct_answer="2", points=1
        )
        self.attempt = Attempt.objects.create(
            student=self.student, variant=self.variant, is_finished=True, score=1, max_score=1, grade="5"
        )

    def test_export_results_docx(self):
        resp = self.client.get("/admin/export/docx/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wordprocessingml", resp["Content-Type"])

    def test_variant_print_student_docx(self):
        resp = self.client.get(f"/admin/variants/{self.variant.id}/print/student/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wordprocessingml", resp["Content-Type"])
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_variant_print_teacher_docx(self):
        resp = self.client.get(f"/admin/variants/{self.variant.id}/print/teacher/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wordprocessingml", resp["Content-Type"])

    def test_variants_print_zip(self):
        resp = self.client.post("/admin/variants/print-zip/", {"ids": [str(self.variant.id)]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("zip", resp["Content-Type"])

    def test_variants_archive_export(self):
        resp = self.client.post("/admin/variants/archive/export/", {"ids": [str(self.variant.id)]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("zip", resp["Content-Type"])


class ManualGradingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin_mg", password="pass", is_staff=True)
        self.admin_client = Client()
        self.admin_client.login(username="admin_mg", password="pass")

        self.school_class = SchoolClass.objects.create(name="11Б", exam_type=ExamType.EGE_PROFILE)
        self.student = Student(full_name="Ручников Ручник", school_class=self.school_class)
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(number="manual_v1", exam_type=ExamType.EGE_PROFILE)
        self.task_auto = Task.objects.create(
            variant=self.variant, number="1", text="2+2=?", correct_answer="4", points=1
        )
        self.task_manual = Task.objects.create(
            variant=self.variant, number="2", text="Объясни", correct_answer="", points=3, manual_grading=True
        )

    def _start_and_answer(self):
        client = Client()
        client.post("/login/", {"full_name": "Ручников Ручник", "password": "pass"})
        client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student, is_finished=False)
        Answer.objects.filter(attempt=attempt, task=self.task_auto).update(student_answer="4")
        return client, attempt

    def test_finish_sets_manual_answer_to_none(self):
        client, attempt = self._start_and_answer()
        client.post(f"/exam/finish/{attempt.id}/")

        answer = Answer.objects.get(attempt=attempt, task=self.task_manual)
        self.assertIsNone(answer.is_correct)
        self.assertIsNone(answer.awarded_points)

    def test_grade_answer_awards_points(self):
        from exam.views import _finish_attempt

        client, attempt = self._start_and_answer()
        _finish_attempt(attempt)

        answer = Answer.objects.get(attempt=attempt, task=self.task_manual)
        resp = self.admin_client.post(
            f"/admin/answers/{answer.id}/grade/",
            {"points": "2"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["awarded_points"], 2)
        self.assertEqual(data["score"], 3)

    def test_no_student_input_does_not_break_scoring(self):
        from exam.views import _finish_attempt

        task_no_input = Task.objects.create(
            variant=self.variant,
            number="3",
            text="Задача без ввода",
            correct_answer="",
            points=2,
            manual_grading=True,
            no_student_input=True,
        )
        attempt = Attempt.objects.create(
            student=self.student, variant=self.variant, is_finished=False, max_score=6
        )
        Answer.objects.create(attempt=attempt, task=self.task_auto, student_answer="4")
        Answer.objects.create(attempt=attempt, task=self.task_manual, student_answer="объяснение")
        Answer.objects.create(attempt=attempt, task=task_no_input)

        _finish_attempt(attempt)
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_finished)

        no_input_answer = Answer.objects.get(attempt=attempt, task=task_no_input)
        self.assertIsNone(no_input_answer.is_correct)


class BulkGradeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin_bg", password="pass", is_staff=True)
        self.client = Client()
        self.client.login(username="admin_bg", password="pass")

        self.school_class = SchoolClass.objects.create(name="9Д", exam_type=ExamType.OGE)
        self.student = Student(full_name="Массовый Проверяемый", school_class=self.school_class)
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(number="bulk_v1", exam_type=ExamType.OGE)
        self.task = Task.objects.create(
            variant=self.variant, number="1", text="Объясни", correct_answer="", points=3, manual_grading=True
        )
        self.attempt = Attempt.objects.create(
            student=self.student, variant=self.variant, is_finished=True, score=0, max_score=3
        )
        self.answer = Answer.objects.create(attempt=self.attempt, task=self.task, student_answer="объяснение")

    def test_bulk_grade_updates_score(self):
        resp = self.client.post(
            f"/admin/classes/{self.school_class.id}/bulk-grade/",
            {
                "attempt_ids": [str(self.attempt.id)],
                f"answer_{self.answer.id}": "2",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.awarded_points, 2)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score, 2)
