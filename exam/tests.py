import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    Answer,
    Attempt,
    ExamType,
    SchoolClass,
    Student,
    Task,
    TaskSource,
    Variant,
)
from .parser import _strip_measurement_unit
from .utils import check_answer, get_grade, normalize_answer


class NormalizeAnswerTests(TestCase):
    def test_integer(self):
        self.assertEqual(normalize_answer("42"), "42")

    def test_decimal_comma(self):
        self.assertEqual(normalize_answer("3,5"), "3.5")

    def test_fraction(self):
        self.assertEqual(normalize_answer("1/2"), "0.5")

    def test_strip_spaces(self):
        self.assertEqual(normalize_answer("  7  "), "7")

    def test_empty(self):
        self.assertEqual(normalize_answer(""), "")

    def test_text(self):
        self.assertEqual(normalize_answer("abc"), "abc")


class CheckAnswerTests(TestCase):
    def test_exact(self):
        self.assertTrue(check_answer("42", "42"))

    def test_comma_vs_dot(self):
        self.assertTrue(check_answer("3,5", "3.5"))

    def test_wrong(self):
        self.assertFalse(check_answer("41", "42"))

    def test_fraction_match(self):
        self.assertTrue(check_answer("1/4", "0.25"))

    def test_empty_answer_is_wrong(self):
        self.assertFalse(check_answer("", "42"))

    def test_pipe_alternatives_first(self):
        # Ответ — первый из вариантов через |
        self.assertTrue(check_answer("234", "234|243|324"))

    def test_pipe_alternatives_last(self):
        # Ответ — последний из вариантов
        self.assertTrue(check_answer("324", "234|243|324"))

    def test_pipe_alternatives_wrong(self):
        # Ответа нет ни в одном варианте
        self.assertFalse(check_answer("999", "234|243|324"))


class StripUnitTests(TestCase):
    """Тесты удаления единиц измерения из ответов."""

    def test_mm(self):
        self.assertEqual(_strip_measurement_unit("0.4 мм"), "0.4")

    def test_km_h(self):
        self.assertEqual(_strip_measurement_unit("60 км/ч"), "60")

    def test_percent(self):
        self.assertEqual(_strip_measurement_unit("15%"), "15")

    def test_rub(self):
        self.assertEqual(_strip_measurement_unit("1200 руб"), "1200")

    def test_no_unit(self):
        # Без единицы — возвращается без изменений
        self.assertEqual(_strip_measurement_unit("42"), "42")

    def test_text_answer(self):
        # Текст не трогается
        self.assertEqual(_strip_measurement_unit("нет"), "нет")

    def test_decimal_with_unit(self):
        self.assertEqual(_strip_measurement_unit("3,5 кг"), "3,5")


class GradeTests(TestCase):
    def test_oge_grade_5(self):
        self.assertEqual(get_grade("oge", 25), "5")

    def test_oge_grade_2(self):
        self.assertEqual(get_grade("oge", 3), "2")

    def test_ege_profile(self):
        self.assertEqual(get_grade("ege_profile", 15), "72")

    def test_ege_base_grade_3(self):
        self.assertEqual(get_grade("ege_base", 8), "3")


class AuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(
            name="9А", exam_type=ExamType.OGE
        )
        self.student = Student(
            full_name="Тестов Тест Тестович",
            school_class=self.school_class,
        )
        self.student.set_password("test123")
        self.student.save()

    def test_login_success(self):
        resp = self.client.post("/login/", {
            "full_name": "Тестов Тест Тестович",
            "password": "test123",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("student_id", self.client.session)

    def test_login_wrong_password(self):
        resp = self.client.post("/login/", {
            "full_name": "Тестов Тест Тестович",
            "password": "wrong",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("student_id", self.client.session)

    def test_logout_requires_post(self):
        resp = self.client.get("/logout/")
        self.assertEqual(resp.status_code, 405)

    def test_logout_post(self):
        self.client.post("/login/", {
            "full_name": "Тестов Тест Тестович",
            "password": "test123",
        })
        resp = self.client.post("/logout/")
        self.assertEqual(resp.status_code, 302)

    def test_protected_page_redirect(self):
        resp = self.client.get("/choose/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url)


class ExamFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(
            name="9Б", exam_type=ExamType.OGE
        )
        self.student = Student(
            full_name="Экзаменов Экзамен",
            school_class=self.school_class,
        )
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(
            number="test001", exam_type=ExamType.OGE, max_attempts=2,
        )
        self.task1 = Task.objects.create(
            variant=self.variant, number=1, text="2+2=?",
            correct_answer="4", points=1,
        )
        self.task2 = Task.objects.create(
            variant=self.variant, number=2, text="3+3=?",
            correct_answer="6", points=1,
        )

        self.client.post("/login/", {
            "full_name": "Экзаменов Экзамен",
            "password": "pass",
        })

    def test_start_exam_creates_attempt(self):
        resp = self.client.get(f"/exam/{self.variant.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Attempt.objects.filter(student=self.student).count(), 1)

    def test_save_answer(self):
        self.client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student)
        answer = Answer.objects.get(attempt=attempt, task=self.task1)

        resp = self.client.post(
            "/exam/save-answer/",
            json.dumps({"answer_id": answer.id, "value": "4"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        answer.refresh_from_db()
        self.assertEqual(answer.student_answer, "4")

    def test_finish_exam_grades(self):
        self.client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student)

        a1 = Answer.objects.get(attempt=attempt, task=self.task1)
        a1.student_answer = "4"
        a1.save()
        a2 = Answer.objects.get(attempt=attempt, task=self.task2)
        a2.student_answer = "5"  # wrong
        a2.save()

        resp = self.client.post(f"/exam/finish/{attempt.id}/")
        self.assertEqual(resp.status_code, 302)

        attempt.refresh_from_db()
        self.assertTrue(attempt.is_finished)
        self.assertEqual(attempt.score, 1)

    def test_attempt_limit(self):
        self.client.get(f"/exam/{self.variant.id}/")
        attempt1 = Attempt.objects.get(student=self.student, is_finished=False)
        self.client.post(f"/exam/finish/{attempt1.id}/")

        self.client.get(f"/exam/{self.variant.id}/")
        attempt2 = Attempt.objects.get(student=self.student, is_finished=False)
        self.client.post(f"/exam/finish/{attempt2.id}/")

        # Третья попытка — лимит (max_attempts=2)
        resp = self.client.get(f"/exam/{self.variant.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Attempt.objects.filter(student=self.student, is_finished=True).count(), 2
        )
        self.assertEqual(
            Attempt.objects.filter(student=self.student, is_finished=False).count(), 0
        )


class AdminTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            "admin", password="admin123", is_staff=True,
        )

    def test_admin_login(self):
        resp = self.client.post("/admin/", {
            "username": "admin",
            "password": "admin123",
        })
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
