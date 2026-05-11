import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from ..models import Attempt, ExamType, SchoolClass, Student, Task, Variant


def _admin_client(username="adm_api"):
    client = Client()
    User.objects.create_user(username, password="pass", is_staff=True)
    client.login(username=username, password="pass")
    return client


def _setup_attempt(variant_number="api_v1", exam_type=ExamType.OGE, class_name="10А"):
    cls = SchoolClass.objects.create(name=class_name, exam_type=exam_type)
    student = Student(full_name="Апиев Апи", school_class=cls)
    student.set_password("p")
    student.save()
    variant = Variant.objects.create(number=variant_number, exam_type=exam_type)
    Task.objects.create(variant=variant, number="1", text="q", correct_answer="1", points=1)
    attempt = Attempt.objects.create(
        student=student,
        variant=variant,
        is_finished=True,
        score=1,
        max_score=1,
    )
    return attempt


class ApiNewAttemptsTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.attempt = _setup_attempt()

    def test_returns_json(self):
        resp = self.client.get("/admin/api/new-attempts/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("count", data)
        self.assertIn("attempts", data)

    def test_recent_attempt_included(self):
        Attempt.objects.filter(id=self.attempt.id).update(finished_at=timezone.now())
        since = (timezone.now() - timedelta(minutes=1)).isoformat()
        resp = self.client.get(f"/admin/api/new-attempts/?since={since}")
        data = json.loads(resp.content)
        self.assertGreater(data["count"], 0)

    def test_old_attempt_excluded(self):
        Attempt.objects.filter(id=self.attempt.id).update(finished_at=timezone.now() - timedelta(hours=25))
        since = (timezone.now() - timedelta(hours=24)).isoformat()
        resp = self.client.get(f"/admin/api/new-attempts/?since={since}")
        data = json.loads(resp.content)
        self.assertEqual(data["count"], 0)

    def test_invalid_since_falls_back_to_24h(self):
        resp = self.client.get("/admin/api/new-attempts/?since=not-a-date")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("count", data)

    def test_unauthenticated_redirects(self):
        client = Client()
        resp = client.get("/admin/api/new-attempts/")
        self.assertNotEqual(resp.status_code, 200)


class AttemptDeleteTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_api2")
        self.attempt = _setup_attempt(variant_number="del_api_v1", class_name="10Б")

    def test_delete_removes_attempt(self):
        student_id = self.attempt.student_id
        resp = self.client.post(f"/admin/attempts/{self.attempt.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Attempt.objects.filter(id=self.attempt.id).exists())
        self.assertIn(str(student_id), resp.url)

    def test_delete_get_not_allowed(self):
        resp = self.client.get(f"/admin/attempts/{self.attempt.id}/delete/")
        self.assertEqual(resp.status_code, 405)

    def test_delete_nonexistent_returns_404(self):
        resp = self.client.post("/admin/attempts/99999/delete/")
        self.assertEqual(resp.status_code, 404)


class VariantImportStatusTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_api3")

    def test_unknown_job_json(self):
        resp = self.client.get("/admin/variants/import/nonexistent-job/status/?json=1")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "unknown")

    def test_unknown_job_html(self):
        resp = self.client.get("/admin/variants/import/nonexistent-job/status/")
        self.assertEqual(resp.status_code, 200)

    def test_import_invalid_url_shows_error(self):
        resp = self.client.post(
            "/admin/variants/import/",
            {"url": "https://example.com/not-sdamgia", "variant_number": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "sdamgia.ru")

    def test_import_empty_url_shows_error(self):
        resp = self.client.post(
            "/admin/variants/import/",
            {"url": "", "variant_number": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите URL")
