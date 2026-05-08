"""
Тесты для exam/services/variant_archive.py
"""

import io
import json
import zipfile

from django.core.files.base import ContentFile
from django.test import TestCase

from ..models import ExamType, Task, TaskImage, Variant
from ..services.variant_archive import (
    ArchiveImportError,
    export_variants_to_zip,
    import_variants_from_zip,
)

# Минимальный 1×1 PNG в байтах (валидный PNG-файл)
_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_variant(number="Вариант 1", exam_type=ExamType.OGE) -> Variant:
    return Variant.objects.create(number=number, exam_type=exam_type, is_active=True, max_attempts=3)


def _make_task(variant, number="1", **kwargs) -> Task:
    defaults = dict(text="Текст", correct_answer="42", source="manual", points=1)
    defaults.update(kwargs)
    return Task.objects.create(variant=variant, number=number, **defaults)


def _zip_from_buf(buf: io.BytesIO) -> zipfile.ZipFile:
    buf.seek(0)
    return zipfile.ZipFile(buf)


class ExportTests(TestCase):
    """Тесты функции export_variants_to_zip."""

    def test_export_variant_no_images(self):
        """Тест 1: экспорт варианта без изображений содержит manifest и variant.json."""
        v = _make_variant()
        _make_task(v)

        buf = export_variants_to_zip(Variant.objects.filter(id=v.id))
        zf = _zip_from_buf(buf)
        names = zf.namelist()

        self.assertIn("manifest.json", names)
        manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["variants_count"], 1)

        folder = manifest["variants"][0]["folder"]
        self.assertIn(f"{folder}/variant.json", names)
        vdata = json.loads(zf.read(f"{folder}/variant.json"))
        self.assertEqual(vdata["number"], v.number)
        self.assertEqual(vdata["exam_type"], v.exam_type)

    def test_export_with_main_image(self):
        """Тест 2: экспорт варианта с Task.image — изображение есть в ZIP."""
        v = _make_variant()
        task = _make_task(v)
        task.image.save("main.png", ContentFile(_MINIMAL_PNG), save=True)

        buf = export_variants_to_zip(Variant.objects.filter(id=v.id))
        zf = _zip_from_buf(buf)
        names = zf.namelist()

        manifest = json.loads(zf.read("manifest.json"))
        folder = manifest["variants"][0]["folder"]
        tdata = json.loads(zf.read(f"{folder}/tasks/1/task.json"))

        self.assertIsNotNone(tdata["image"])
        img_path = f"{folder}/tasks/1/{tdata['image']}"
        self.assertIn(img_path, names)

    def test_export_with_shared_context_image(self):
        """Тест 3: экспорт с shared_context_image — изображение есть в ZIP."""
        v = _make_variant()
        task = _make_task(v)
        task.shared_context_image.save("sc.png", ContentFile(_MINIMAL_PNG), save=True)

        buf = export_variants_to_zip(Variant.objects.filter(id=v.id))
        zf = _zip_from_buf(buf)
        manifest = json.loads(zf.read("manifest.json"))
        folder = manifest["variants"][0]["folder"]
        tdata = json.loads(zf.read(f"{folder}/tasks/1/task.json"))

        self.assertIsNotNone(tdata["shared_context_image"])
        img_path = f"{folder}/tasks/1/{tdata['shared_context_image']}"
        self.assertIn(img_path, zf.namelist())

    def test_export_with_task_image(self):
        """Тест 4: экспорт с TaskImage — extra-изображения есть в ZIP."""
        v = _make_variant()
        task = _make_task(v)
        ti = TaskImage(task=task, order=0)
        ti.image.save("extra.png", ContentFile(_MINIMAL_PNG), save=True)

        buf = export_variants_to_zip(Variant.objects.filter(id=v.id))
        zf = _zip_from_buf(buf)
        manifest = json.loads(zf.read("manifest.json"))
        folder = manifest["variants"][0]["folder"]
        tdata = json.loads(zf.read(f"{folder}/tasks/1/task.json"))

        self.assertEqual(len(tdata["extra_images"]), 1)
        extra_path = f"{folder}/tasks/1/{tdata['extra_images'][0]}"
        self.assertIn(extra_path, zf.namelist())

    def test_export_multiple_variants(self):
        """Тест 5: экспорт нескольких вариантов — папки для каждого в ZIP."""
        v1 = _make_variant("В1")
        v2 = _make_variant("В2")
        _make_task(v1)
        _make_task(v2)

        buf = export_variants_to_zip(Variant.objects.filter(id__in=[v1.id, v2.id]))
        zf = _zip_from_buf(buf)
        manifest = json.loads(zf.read("manifest.json"))

        self.assertEqual(manifest["variants_count"], 2)
        folders = [vm["folder"] for vm in manifest["variants"]]
        self.assertEqual(len(set(folders)), 2)  # уникальные папки


class ImportTests(TestCase):
    """Тесты функции import_variants_from_zip."""

    def _build_minimal_zip(self, number="Вариант 1", exam_type="oge") -> io.BytesIO:
        """Строит минимальный валидный архив с одним вариантом и одним заданием."""
        buf = io.BytesIO()
        folder = number.replace("/", "_")

        manifest = {
            "format_version": 1,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "app": "EGE",
            "variants_count": 1,
            "variants": [{"folder": folder, "number": number, "exam_type": exam_type}],
        }
        vdata = {
            "format_version": 1,
            "number": number,
            "exam_type": exam_type,
            "max_attempts": 3,
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
        return buf

    def _uploaded(self, buf: io.BytesIO):
        """Оборачивает BytesIO в объект с методом read() и name."""
        buf.seek(0)

        class FakeFile:
            def read(self):
                return buf.read()

        return FakeFile()

    def test_import_creates_inactive_variants(self):
        """Тест 6: импорт создаёт варианты с is_active=False."""
        result = import_variants_from_zip(self._uploaded(self._build_minimal_zip()))

        self.assertEqual(result["variants_created"], 1)
        self.assertEqual(result["tasks_created"], 1)
        v = Variant.objects.get(number="Вариант 1")
        self.assertFalse(v.is_active)

    def test_import_conflict_creates_renamed_variant(self):
        """Тест 7: конфликт номера → новый вариант с суффиксом."""
        Variant.objects.create(number="Вариант 1", exam_type=ExamType.OGE)
        result = import_variants_from_zip(self._uploaded(self._build_minimal_zip()))

        self.assertEqual(result["variants_created"], 1)
        self.assertEqual(len(result["renamed"]), 1)
        self.assertIn("Вариант 1 →", result["renamed"][0])
        # Исходный вариант не перезаписан
        self.assertEqual(Variant.objects.filter(number="Вариант 1").count(), 1)

    def test_import_does_not_create_attempts_or_answers(self):
        """Тест 8: импорт не создаёт Attempt или Answer."""
        from ..models import Answer, Attempt

        import_variants_from_zip(self._uploaded(self._build_minimal_zip()))

        self.assertEqual(Attempt.objects.count(), 0)
        self.assertEqual(Answer.objects.count(), 0)

    def test_import_broken_zip_raises_error(self):
        """Тест 9: повреждённый ZIP → ArchiveImportError, ничего не создано."""
        broken = io.BytesIO(b"not a zip file at all")

        class FakeFile:
            def read(self):
                return broken.read()

        with self.assertRaises(ArchiveImportError):
            import_variants_from_zip(FakeFile())

        self.assertEqual(Variant.objects.count(), 0)

    def test_import_path_traversal_rejected(self):
        """Тест 10: ZIP с ../file в пути → ArchiveImportError."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format_version": 1, "variants": []}))
            # Принудительно добавляем опасный путь
            info = zipfile.ZipInfo("../../etc/passwd")
            zf.writestr(info, "root:x:0:0")
        buf.seek(0)

        class FakeFile:
            def read(self):
                return buf.read()

        with self.assertRaises(ArchiveImportError):
            import_variants_from_zip(FakeFile())
