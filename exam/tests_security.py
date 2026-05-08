import io
import json
import zipfile

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import Answer, Attempt, ExamType, SchoolClass, Student, Task, TaskSource, Variant
from .parser import sanitize_html
from .utils import compute_task_stats


class SecurityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(name="10Б", exam_type=ExamType.EGE_PROFILE)
        self.student1 = Student(full_name="Тест Один", school_class=self.school_class)
        self.student1.set_password("pass1")
        self.student1.save()
        self.student2 = Student(full_name="Тест Два", school_class=self.school_class)
        self.student2.set_password("pass2")
        self.student2.save()
        self.variant = Variant.objects.create(number="sec_v1", exam_type=ExamType.EGE_PROFILE)
        self.task = Task.objects.create(
            variant=self.variant,
            number="1",
            text="Задание",
            correct_answer="42",
        )

    def _login(self, student):
        session = self.client.session
        session["student_id"] = student.id
        session.save()
        student.session_key = self.client.session.session_key
        student.save(update_fields=["session_key"])

    def test_student_cannot_access_other_results(self):
        attempt = Attempt.objects.create(
            student=self.student2,
            variant=self.variant,
            is_finished=True,
            score=1,
            max_score=1,
        )
        self._login(self.student1)
        resp = self.client.get(f"/results/{attempt.id}/")
        self.assertNotEqual(resp.status_code, 200)

    def test_sanitize_html_strips_script_tags(self):
        xss = '<script>alert("xss")</script>текст задания'
        result = sanitize_html(xss)
        self.assertNotIn("<script>", result)
        self.assertIn("текст задания", result)

    def test_sanitize_html_strips_event_handlers(self):
        xss = '<img src="x" onerror="alert(1)">'
        result = sanitize_html(xss)
        self.assertNotIn("onerror", result)

    def test_attempt_limit_enforced(self):
        self.variant.max_attempts = 1
        self.variant.save()
        Attempt.objects.create(
            student=self.student1,
            variant=self.variant,
            is_finished=True,
            score=0,
            max_score=1,
        )
        self._login(self.student1)
        resp = self.client.get(f"/start/{self.variant.id}/")
        self.assertNotEqual(resp.status_code, 302)
        self.assertEqual(Attempt.objects.filter(student=self.student1, is_finished=False).count(), 0)

    def test_pdf_size_limit_rejected(self):
        User.objects.create_user("admin_sec", password="pass", is_staff=True)
        self.client.login(username="admin_sec", password="pass")
        big_file = io.BytesIO(b"0" * (51 * 1024 * 1024))
        big_file.name = "big.pdf"
        big_file.size = 51 * 1024 * 1024
        resp = self.client.post(
            "/admin/catalog/import-pdf/",
            {
                "exam_type": ExamType.EGE_PROFILE,
                "pdf_files": big_file,
                "mode": "catalog",
                "format": "print_solve",
            },
        )
        self.assertContains(resp, "слишком большой")

    def test_compute_task_stats_empty(self):
        result = compute_task_stats(Answer.objects.none())
        self.assertEqual(result, {})

    def test_sanitize_html_onerror_unquoted(self):
        result = sanitize_html("<img src=x onerror=alert(1)>")
        self.assertNotIn("onerror", result)

    def test_sanitize_html_onerror_spaces_around_equals(self):
        result = sanitize_html("<img src=x onerror = alert(1)>")
        self.assertNotIn("onerror", result)

    def test_sanitize_html_javascript_href(self):
        result = sanitize_html('<a href="javascript:alert(1)">click</a>')
        self.assertNotIn("javascript:", result)

    def test_sanitize_html_javascript_src(self):
        result = sanitize_html('<img src="javascript:alert(1)">')
        self.assertNotIn("javascript:", result)

    def test_zip_import_text_not_sanitized(self):
        from exam.services.variant_archive import import_variants_from_zip

        xss = "<img src=x onerror=alert(1)>"
        folder = "xss_zip"
        manifest = {
            "format_version": 1,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "app": "EGE",
            "variants_count": 1,
            "variants": [{"folder": folder, "number": "xss_zip_v", "exam_type": "oge"}],
        }
        vdata = {
            "format_version": 1,
            "number": "xss_zip_v",
            "exam_type": "oge",
            "max_attempts": 1,
            "is_active": True,
            "tasks": [{"folder": "tasks/1", "number": "1"}],
        }
        tdata = {
            "format_version": 1,
            "number": "1",
            "text": xss,
            "correct_answer": "1",
            "source": "manual",
            "points": 1,
            "manual_grading": False,
            "no_student_input": False,
            "shared_context": "",
            "image": None,
            "shared_context_image": None,
            "extra_images": [],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr(f"{folder}/variant.json", json.dumps(vdata))
            zf.writestr(f"{folder}/tasks/1/task.json", json.dumps(tdata))
        buf.seek(0)

        class FakeFile:
            def read(self):
                return buf.read()

        import_variants_from_zip(FakeFile())
        task = Task.objects.get(variant__number="xss_zip_v")
        self.assertEqual(task.text, xss)
