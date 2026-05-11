from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import Attempt, ExamType, SchoolClass, Student, Task, Variant


def _admin_client(username="adm_cls"):
    client = Client()
    User.objects.create_user(username, password="pass", is_staff=True)
    client.login(username=username, password="pass")
    return client


class ClassAddTests(TestCase):
    def setUp(self):
        self.client = _admin_client()

    def test_add_class_creates_and_redirects(self):
        resp = self.client.post(
            "/admin/classes/add/",
            {"name": "9А", "exam_type": ExamType.OGE},
        )
        self.assertRedirects(resp, "/admin/classes/", fetch_redirect_response=False)
        self.assertTrue(SchoolClass.objects.filter(name="9А").exists())

    def test_add_class_empty_name_shows_error(self):
        resp = self.client.post(
            "/admin/classes/add/",
            {"name": "", "exam_type": ExamType.OGE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите название")

    def test_add_class_invalid_exam_type_shows_error(self):
        resp = self.client.post(
            "/admin/classes/add/",
            {"name": "9Б", "exam_type": "unknown"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Выберите тип")

    def test_add_class_duplicate_shows_error(self):
        SchoolClass.objects.create(name="9В", exam_type=ExamType.OGE)
        resp = self.client.post(
            "/admin/classes/add/",
            {"name": "9В", "exam_type": ExamType.OGE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже существует")

    def test_get_add_returns_200(self):
        resp = self.client.get("/admin/classes/add/")
        self.assertEqual(resp.status_code, 200)


class ClassEditTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.cls = SchoolClass.objects.create(name="10А", exam_type=ExamType.EGE_PROFILE)

    def test_edit_class_updates_and_redirects(self):
        resp = self.client.post(
            f"/admin/classes/{self.cls.id}/edit/",
            {"name": "10Б", "exam_type": ExamType.OGE},
        )
        self.assertRedirects(resp, "/admin/classes/", fetch_redirect_response=False)
        self.cls.refresh_from_db()
        self.assertEqual(self.cls.name, "10Б")
        self.assertEqual(self.cls.exam_type, ExamType.OGE)

    def test_edit_class_empty_name_shows_error(self):
        resp = self.client.post(
            f"/admin/classes/{self.cls.id}/edit/",
            {"name": "", "exam_type": ExamType.OGE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите название")

    def test_get_edit_returns_200(self):
        resp = self.client.get(f"/admin/classes/{self.cls.id}/edit/")
        self.assertEqual(resp.status_code, 200)


class ClassDeleteTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.cls = SchoolClass.objects.create(name="11А", exam_type=ExamType.EGE_PROFILE)

    def test_delete_removes_class(self):
        resp = self.client.post(f"/admin/classes/{self.cls.id}/delete/")
        self.assertRedirects(resp, "/admin/classes/", fetch_redirect_response=False)
        self.assertFalse(SchoolClass.objects.filter(id=self.cls.id).exists())

    def test_delete_get_not_allowed(self):
        resp = self.client.get(f"/admin/classes/{self.cls.id}/delete/")
        self.assertEqual(resp.status_code, 405)


class ClassToggleTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.cls = SchoolClass.objects.create(name="11Б", exam_type=ExamType.EGE_PROFILE, is_active=True)

    def test_toggle_deactivates(self):
        self.client.post(f"/admin/classes/{self.cls.id}/toggle/")
        self.cls.refresh_from_db()
        self.assertFalse(self.cls.is_active)

    def test_toggle_activates(self):
        self.cls.is_active = False
        self.cls.save()
        self.client.post(f"/admin/classes/{self.cls.id}/toggle/")
        self.cls.refresh_from_db()
        self.assertTrue(self.cls.is_active)


class ClassStatsTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.cls = SchoolClass.objects.create(name="11В", exam_type=ExamType.EGE_PROFILE)
        self.student = Student(full_name="Стат Статов", school_class=self.cls)
        self.student.set_password("pass")
        self.student.save()
        self.variant = Variant.objects.create(number="cs_v1", exam_type=ExamType.EGE_PROFILE)
        Task.objects.create(variant=self.variant, number=1, text="q", correct_answer="1", points=1)

    def test_stats_empty_returns_200(self):
        resp = self.client.get(f"/admin/classes/{self.cls.id}/stats/")
        self.assertEqual(resp.status_code, 200)

    def test_stats_with_attempts_returns_200(self):
        Attempt.objects.create(
            student=self.student,
            variant=self.variant,
            is_finished=True,
            score=1,
            max_score=1,
        )
        resp = self.client.get(f"/admin/classes/{self.cls.id}/stats/")
        self.assertEqual(resp.status_code, 200)

    def test_stats_nonexistent_class_returns_404(self):
        resp = self.client.get("/admin/classes/99999/stats/")
        self.assertEqual(resp.status_code, 404)
