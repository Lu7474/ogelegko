import io
import json
import zipfile

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import CatalogTask, CatalogTaskImage, ExamType, Task, TaskSource, Variant


class VariantFromCatalogImageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user("admin2", password="admin123", is_staff=True)
        self.client.post("/admin/", {"username": "admin2", "password": "admin123"})

    def test_extra_images_copied(self):
        from django.core.files.base import ContentFile

        ct = CatalogTask.objects.create(
            task_number=1,
            exam_type=ExamType.OGE,
            text="задание",
            correct_answer="1",
            source=TaskSource.MANUAL,
        )
        minimal_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        ci = CatalogTaskImage(task=ct, order=0)
        ci.image.save("test.png", ContentFile(minimal_png), save=True)

        resp = self.client.post(
            "/admin/variants/from-catalog/",
            {
                "variant_number": "from_catalog_img_test",
                "exam_type": "oge",
                "selected_tasks": json.dumps({"1": ct.id}),
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data.get("ok"))

        task = Task.objects.get(variant__number="from_catalog_img_test")
        self.assertEqual(task.extra_images.count(), 1)


class CatalogTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user("admin_cat", password="pass", is_staff=True)
        self.client.login(username="admin_cat", password="pass")

    def test_variant_auto_generate_creates_variant(self):
        for n in range(1, 19):
            CatalogTask.objects.create(
                task_number=n,
                exam_type=ExamType.EGE_PROFILE,
                text=f"Задание {n}",
                correct_answer=str(n),
                source=TaskSource.MANUAL,
            )
        resp = self.client.post(
            "/admin/variants/auto-generate/",
            {"variant_number": "auto_gen_test", "exam_type": "ege_profile", "strategy": "latest"},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data.get("ok"), data)
        variant = Variant.objects.get(number="auto_gen_test")
        self.assertEqual(variant.tasks.count(), 18)

    def test_fipi_status_unknown_job_returns_json(self):
        resp = self.client.get("/admin/catalog/import-fipi/nonexistent-job/status/?json=1")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data.get("status"), "unknown")

    def test_pdf_import_status_unknown_job_returns_json(self):
        resp = self.client.get("/admin/catalog/import-pdf/nonexistent-job/status/?json=1")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data.get("status"), "unknown")

    def test_fipi_export_answers_returns_file(self):
        CatalogTask.objects.create(
            task_number=1,
            exam_type=ExamType.OGE,
            text="Задание",
            correct_answer="",
            source=TaskSource.FIPI,
            fipi_guid="test-guid-001",
        )
        resp = self.client.get("/admin/catalog/fipi-answers/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("json", resp["Content-Type"])
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_fipi_import_answers_updates_correct_answer(self):
        task = CatalogTask.objects.create(
            task_number=1,
            exam_type=ExamType.OGE,
            text="Задание",
            correct_answer="",
            source=TaskSource.FIPI,
            fipi_guid="test-guid-002",
        )
        payload = json.dumps([{"fipi_guid": "test-guid-002", "answer": "42"}]).encode()
        resp = self.client.post(
            "/admin/catalog/fipi-answers/import/",
            {"answers_file": io.BytesIO(payload)},
        )
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.correct_answer, "42")

    def test_catalog_bulk_delete(self):
        t1 = CatalogTask.objects.create(
            task_number=1, exam_type=ExamType.OGE, text="т1", correct_answer="1", source=TaskSource.MANUAL
        )
        t2 = CatalogTask.objects.create(
            task_number=2, exam_type=ExamType.OGE, text="т2", correct_answer="2", source=TaskSource.MANUAL
        )
        resp = self.client.post(
            "/admin/catalog/bulk-delete/",
            {"ids": [str(t1.id), str(t2.id)]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CatalogTask.objects.filter(id__in=[t1.id, t2.id]).count(), 0)


class ArchiveImportViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user("admin_ai", password="pass", is_staff=True)
        self.client.login(username="admin_ai", password="pass")

    def _build_zip(self, number="Import Test"):
        buf = io.BytesIO()
        folder = number.replace(" ", "_")
        manifest = {
            "format_version": 1,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "app": "EGE",
            "variants_count": 1,
            "variants": [{"folder": folder, "number": number, "exam_type": "oge"}],
        }
        vdata = {
            "format_version": 1,
            "number": number,
            "exam_type": "oge",
            "max_attempts": 2,
            "is_active": True,
            "tasks": [{"folder": "tasks/1", "number": "1"}],
        }
        tdata = {
            "format_version": 1,
            "number": "1",
            "text": "Текст задания",
            "correct_answer": "42",
            "source": "manual",
            "points": 1,
            "manual_grading": False,
            "no_student_input": False,
            "shared_context": "",
            "image": None,
            "shared_context_image": None,
            "extra_images": [],
        }
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr(f"{folder}/variant.json", json.dumps(vdata))
            zf.writestr(f"{folder}/tasks/1/task.json", json.dumps(tdata))
        buf.seek(0)
        buf.name = "archive.zip"
        return buf

    def test_import_creates_variant(self):
        resp = self.client.post(
            "/admin/variants/archive/import/",
            {"archive": self._build_zip()},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Variant.objects.filter(number="Import Test").exists())

    def test_import_broken_zip_redirects_with_error(self):
        broken = io.BytesIO(b"not a zip")
        broken.name = "bad.zip"
        resp = self.client.post("/admin/variants/archive/import/", {"archive": broken})
        self.assertEqual(resp.status_code, 302)
