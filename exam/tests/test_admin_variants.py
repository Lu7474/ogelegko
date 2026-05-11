from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import Attempt, ExamType, SchoolClass, Student, Task, TaskSource, Variant


def _admin_client(username="adm_var"):
    client = Client()
    User.objects.create_user(username, password="pass", is_staff=True)
    client.login(username=username, password="pass")
    return client


def _make_variant(number="v1", exam_type=ExamType.OGE, is_active=True):
    return Variant.objects.create(number=number, exam_type=exam_type, is_active=is_active)


class VariantAddTests(TestCase):
    def setUp(self):
        self.client = _admin_client()

    def test_add_variant_creates_and_redirects(self):
        resp = self.client.post(
            "/admin/variants/add/",
            {
                "number": "new_v1",
                "exam_type": ExamType.OGE,
                "max_attempts": "3",
                "task_1_answer": "42",
                "task_1_text": "Вопрос",
                "task_1_source": TaskSource.MANUAL,
                "task_1_points": "1",
            },
        )
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.assertTrue(Variant.objects.filter(number="new_v1").exists())

    def test_add_variant_empty_number_shows_error(self):
        resp = self.client.post(
            "/admin/variants/add/",
            {"number": "", "exam_type": ExamType.OGE, "max_attempts": "3"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите номер")

    def test_add_variant_duplicate_shows_error(self):
        _make_variant("dup_v")
        resp = self.client.post(
            "/admin/variants/add/",
            {"number": "dup_v", "exam_type": ExamType.OGE, "max_attempts": "3"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже существует")

    def test_get_add_returns_200(self):
        resp = self.client.get("/admin/variants/add/")
        self.assertEqual(resp.status_code, 200)


class VariantEditTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var2")
        self.variant = _make_variant("edit_v1")
        Task.objects.create(variant=self.variant, number="1", text="q", correct_answer="a", points=1)

    def test_edit_variant_updates_number(self):
        resp = self.client.post(
            f"/admin/variants/{self.variant.id}/edit/",
            {
                "number": "edit_v1_renamed",
                "exam_type": ExamType.OGE,
                "max_attempts": "2",
                "task_1_answer": "a",
                "task_1_text": "q",
                "task_1_source": TaskSource.MANUAL,
                "task_1_points": "1",
            },
        )
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.number, "edit_v1_renamed")

    def test_edit_variant_empty_number_shows_error(self):
        resp = self.client.post(
            f"/admin/variants/{self.variant.id}/edit/",
            {"number": "", "exam_type": ExamType.OGE, "max_attempts": "2"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите номер")

    def test_get_edit_returns_200(self):
        resp = self.client.get(f"/admin/variants/{self.variant.id}/edit/")
        self.assertEqual(resp.status_code, 200)


class VariantDeleteTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var3")
        self.variant = _make_variant("del_v1")

    def test_delete_removes_variant(self):
        resp = self.client.post(f"/admin/variants/{self.variant.id}/delete/")
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.assertFalse(Variant.objects.filter(id=self.variant.id).exists())

    def test_delete_get_not_allowed(self):
        resp = self.client.get(f"/admin/variants/{self.variant.id}/delete/")
        self.assertEqual(resp.status_code, 405)


class VariantToggleTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var4")
        self.variant = _make_variant("tog_v1", is_active=True)

    def test_toggle_deactivates(self):
        self.client.post(f"/admin/variants/{self.variant.id}/toggle/")
        self.variant.refresh_from_db()
        self.assertFalse(self.variant.is_active)

    def test_toggle_activates(self):
        self.variant.is_active = False
        self.variant.save()
        self.client.post(f"/admin/variants/{self.variant.id}/toggle/")
        self.variant.refresh_from_db()
        self.assertTrue(self.variant.is_active)


class VariantDuplicateTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var5")
        self.variant = _make_variant("orig_v1")
        Task.objects.create(variant=self.variant, number="1", text="задание", correct_answer="42", points=2)

    def test_duplicate_creates_copy(self):
        resp = self.client.post(f"/admin/variants/{self.variant.id}/duplicate/")
        self.assertEqual(resp.status_code, 302)
        copy = Variant.objects.filter(number="orig_v1_копия").first()
        self.assertIsNotNone(copy)
        self.assertEqual(copy.tasks.count(), 1)
        self.assertEqual(copy.tasks.first().correct_answer, "42")

    def test_duplicate_twice_uses_counter(self):
        self.client.post(f"/admin/variants/{self.variant.id}/duplicate/")
        self.client.post(f"/admin/variants/{self.variant.id}/duplicate/")
        self.assertTrue(Variant.objects.filter(number="orig_v1_копия2").exists())


class VariantStatsTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var6")
        self.variant = _make_variant("stat_v1")
        Task.objects.create(variant=self.variant, number="1", text="q", correct_answer="1", points=1)
        self.cls = SchoolClass.objects.create(name="10А", exam_type=ExamType.OGE)
        self.student = Student(full_name="Статов Стат", school_class=self.cls)
        self.student.set_password("p")
        self.student.save()

    def test_stats_no_attempts_returns_200(self):
        resp = self.client.get(f"/admin/variants/{self.variant.id}/stats/")
        self.assertEqual(resp.status_code, 200)

    def test_stats_with_attempt_shows_data(self):
        Attempt.objects.create(
            student=self.student,
            variant=self.variant,
            is_finished=True,
            score=1,
            max_score=1,
        )
        resp = self.client.get(f"/admin/variants/{self.variant.id}/stats/")
        self.assertEqual(resp.status_code, 200)


class VariantBulkTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_var7")
        self.v1 = _make_variant("bulk_v1", is_active=True)
        self.v2 = _make_variant("bulk_v2", is_active=True)

    def test_bulk_toggle_hide(self):
        resp = self.client.post(
            "/admin/variants/bulk-toggle/",
            {"ids": [str(self.v1.id), str(self.v2.id)], "action": "hide"},
        )
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.assertFalse(Variant.objects.get(id=self.v1.id).is_active)
        self.assertFalse(Variant.objects.get(id=self.v2.id).is_active)

    def test_bulk_toggle_activate(self):
        self.v1.is_active = False
        self.v1.save()
        resp = self.client.post(
            "/admin/variants/bulk-toggle/",
            {"ids": [str(self.v1.id)], "action": "activate"},
        )
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.assertTrue(Variant.objects.get(id=self.v1.id).is_active)

    def test_bulk_delete_removes_variants(self):
        resp = self.client.post(
            "/admin/variants/bulk-delete/",
            {"ids": [str(self.v1.id), str(self.v2.id)]},
        )
        self.assertRedirects(resp, "/admin/variants/", fetch_redirect_response=False)
        self.assertFalse(Variant.objects.filter(id__in=[self.v1.id, self.v2.id]).exists())

    def test_bulk_delete_get_not_allowed(self):
        resp = self.client.get("/admin/variants/bulk-delete/")
        self.assertEqual(resp.status_code, 405)
