"""
Сервис экспорта/импорта вариантов в переносимый ZIP-архив.

Формат архива:
  manifest.json
  <Имя варианта>/
      variant.json
      tasks/<номер>/
          task.json
          images/
              main.png
              shared_context.png
              extra_1.jpg
"""

import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone

from django.core.files.base import ContentFile
from django.db import transaction

from exam.models import ExamType, Task, TaskImage, Variant

SUPPORTED_FORMAT_VERSION = 1
MAX_ZIP_SIZE_BYTES = 200 * 1024 * 1024  # 200 МБ
MAX_ZIP_FILES = 1000
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_UNSAFE_CHARS_RE = re.compile(r'[/\\:*?"<>|]')


def _safe_folder_name(name: str) -> str:
    """Заменяет символы, опасные для имён папок, на '_'."""
    return _UNSAFE_CHARS_RE.sub("_", name)


def _unique_folder(name: str, used: set) -> str:
    """Возвращает уникальное имя папки, добавляя суффикс при коллизии."""
    candidate = name
    counter = 2
    while candidate in used:
        candidate = f"{name}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _read_image_bytes(field) -> bytes | None:
    """Читает байты из ImageField (работает с локальными файлами и Cloudinary)."""
    if not field or not field.name:
        return None
    try:
        with field.open("rb") as f:
            return f.read()
    except Exception:
        return None


def export_variants_to_zip(variants) -> io.BytesIO:
    """
    Экспортирует переданный queryset вариантов в ZIP-архив.
    Возвращает BytesIO с содержимым ZIP.
    """
    buf = io.BytesIO()
    used_folders: set = set()

    exported_at = datetime.now(tz=timezone.utc).astimezone().isoformat()

    manifest_variants = []

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for variant in variants.prefetch_related("tasks__extra_images"):
            folder = _unique_folder(_safe_folder_name(variant.number), used_folders)

            manifest_variants.append(
                {
                    "folder": folder,
                    "number": variant.number,
                    "exam_type": variant.exam_type,
                }
            )

            # --- variant.json ---
            task_refs = []
            for task in variant.tasks.all():
                task_refs.append({"folder": f"tasks/{task.number}", "number": task.number})

            variant_data = {
                "format_version": SUPPORTED_FORMAT_VERSION,
                "number": variant.number,
                "exam_type": variant.exam_type,
                "max_attempts": variant.max_attempts,
                "is_active": variant.is_active,
                "tasks": task_refs,
            }
            zf.writestr(f"{folder}/variant.json", json.dumps(variant_data, ensure_ascii=False, indent=2))

            # --- задания ---
            for task in variant.tasks.all():
                task_folder = f"{folder}/tasks/{task.number}"

                image_path = None
                shared_image_path = None
                extra_image_paths = []

                # основное изображение
                img_bytes = _read_image_bytes(task.image)
                if img_bytes is not None:
                    ext = os.path.splitext(task.image.name)[1].lower() or ".png"
                    image_path = f"images/main{ext}"
                    zf.writestr(f"{task_folder}/{image_path}", img_bytes)

                # изображение общего условия
                shared_bytes = _read_image_bytes(task.shared_context_image)
                if shared_bytes is not None:
                    ext = os.path.splitext(task.shared_context_image.name)[1].lower() or ".png"
                    shared_image_path = f"images/shared_context{ext}"
                    zf.writestr(f"{task_folder}/{shared_image_path}", shared_bytes)

                # дополнительные изображения
                for idx, extra in enumerate(task.extra_images.all(), start=1):
                    extra_bytes = _read_image_bytes(extra.image)
                    if extra_bytes is not None:
                        ext = os.path.splitext(extra.image.name)[1].lower() or ".png"
                        extra_path = f"images/extra_{idx}{ext}"
                        zf.writestr(f"{task_folder}/{extra_path}", extra_bytes)
                        extra_image_paths.append(extra_path)

                task_data = {
                    "format_version": SUPPORTED_FORMAT_VERSION,
                    "number": task.number,
                    "text": task.text,
                    "correct_answer": task.correct_answer,
                    "source": task.source,
                    "points": task.points,
                    "manual_grading": task.manual_grading,
                    "no_student_input": task.no_student_input,
                    "shared_context": task.shared_context,
                    "image": image_path,
                    "shared_context_image": shared_image_path,
                    "extra_images": extra_image_paths,
                }
                zf.writestr(f"{task_folder}/task.json", json.dumps(task_data, ensure_ascii=False, indent=2))

        # --- manifest.json ---
        manifest = {
            "format_version": SUPPORTED_FORMAT_VERSION,
            "exported_at": exported_at,
            "app": "EGE",
            "variants_count": len(manifest_variants),
            "variants": manifest_variants,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    buf.seek(0)
    return buf


class ArchiveImportError(Exception):
    pass


def _check_path_safe(path: str) -> None:
    """Выбрасывает исключение если путь содержит попытку выйти за пределы архива."""
    norm = os.path.normpath(path)
    if norm.startswith("..") or os.path.isabs(norm):
        raise ArchiveImportError(f"Небезопасный путь в архиве: {path!r}")


def import_variants_from_zip(uploaded_file) -> dict:
    """
    Импортирует варианты из ZIP-архива.

    Возвращает:
        {
            'variants_created': int,
            'tasks_created': int,
            'renamed': list[str],
            'errors': list[str],
        }

    Выбрасывает ArchiveImportError если архив невалиден (до начала транзакции).
    """
    result = {
        "variants_created": 0,
        "tasks_created": 0,
        "renamed": [],
        "errors": [],
    }

    # --- Базовые проверки до открытия транзакции ---
    raw = uploaded_file.read()

    if len(raw) > MAX_ZIP_SIZE_BYTES:
        raise ArchiveImportError(f"Архив слишком большой (максимум {MAX_ZIP_SIZE_BYTES // 1024 // 1024} МБ).")

    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ArchiveImportError("Загруженный файл не является ZIP-архивом.")

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()

        if len(names) > MAX_ZIP_FILES:
            raise ArchiveImportError(f"Архив содержит слишком много файлов (максимум {MAX_ZIP_FILES}).")

        # Проверка путей на безопасность
        for name in names:
            _check_path_safe(name)

        # manifest.json
        if "manifest.json" not in names:
            raise ArchiveImportError("Архив не содержит manifest.json.")

        try:
            manifest = json.loads(zf.read("manifest.json"))
        except Exception:
            raise ArchiveImportError("Не удалось прочитать manifest.json.")

        if manifest.get("format_version") != SUPPORTED_FORMAT_VERSION:
            raise ArchiveImportError(
                f"Неподдерживаемая версия формата: {manifest.get('format_version')}. "
                f"Поддерживается: {SUPPORTED_FORMAT_VERSION}."
            )

        variants_meta = manifest.get("variants", [])
        if not variants_meta:
            raise ArchiveImportError("В архиве нет вариантов.")

        valid_exam_types = {k for k, _ in ExamType.choices}

        # Предварительная валидация всех вариантов
        for vm in variants_meta:
            v_folder = vm.get("folder", "")
            variant_json_path = f"{v_folder}/variant.json"

            if variant_json_path not in names:
                raise ArchiveImportError(f"Отсутствует {variant_json_path}.")

            try:
                vdata = json.loads(zf.read(variant_json_path))
            except Exception:
                raise ArchiveImportError(f"Не удалось прочитать {variant_json_path}.")

            if vdata.get("exam_type") not in valid_exam_types:
                raise ArchiveImportError(
                    f"Недопустимый exam_type={vdata.get('exam_type')!r} в {variant_json_path}."
                )

            for task_ref in vdata.get("tasks", []):
                t_folder = f"{v_folder}/{task_ref['folder']}"
                task_json_path = f"{t_folder}/task.json"
                if task_json_path not in names:
                    raise ArchiveImportError(f"Отсутствует {task_json_path}.")

                try:
                    tdata = json.loads(zf.read(task_json_path))
                except Exception:
                    raise ArchiveImportError(f"Не удалось прочитать {task_json_path}.")

                # Проверка что указанные изображения реально есть в архиве
                for img_key in ("image", "shared_context_image"):
                    img_rel = tdata.get(img_key)
                    if img_rel:
                        img_full = f"{t_folder}/{img_rel}"
                        if img_full not in names:
                            raise ArchiveImportError(
                                f"Изображение {img_full} указано в {task_json_path}, но отсутствует в архиве."
                            )
                        ext = os.path.splitext(img_rel)[1].lower()
                        if ext not in ALLOWED_IMAGE_EXTENSIONS:
                            raise ArchiveImportError(f"Недопустимое расширение изображения: {img_rel!r}.")

                for extra_rel in tdata.get("extra_images", []):
                    extra_full = f"{t_folder}/{extra_rel}"
                    if extra_full not in names:
                        raise ArchiveImportError(
                            f"Изображение {extra_full} указано в {task_json_path}, но отсутствует в архиве."
                        )
                    ext = os.path.splitext(extra_rel)[1].lower()
                    if ext not in ALLOWED_IMAGE_EXTENSIONS:
                        raise ArchiveImportError(f"Недопустимое расширение изображения: {extra_rel!r}.")

        # --- Импорт в транзакции ---
        _import_all(zf, variants_meta, result)

    return result


@transaction.atomic
def _import_all(zf: zipfile.ZipFile, variants_meta: list, result: dict) -> None:
    """Создаёт все объекты в рамках одной атомарной транзакции."""
    now_tag = datetime.now().strftime("%Y%m%d_%H%M")
    existing_numbers = set(Variant.objects.values_list("number", flat=True))

    for vm in variants_meta:
        v_folder = vm["folder"]
        vdata = json.loads(zf.read(f"{v_folder}/variant.json"))

        # Разрешение конфликта номеров
        original_number = vdata["number"]
        number = original_number
        if number in existing_numbers:
            candidate = f"{original_number}_imported_{now_tag}"
            counter = 2
            while candidate in existing_numbers:
                candidate = f"{original_number}_imported_{now_tag}_{counter}"
                counter += 1
            number = candidate
            result["renamed"].append(f"{original_number} → {number}")

        existing_numbers.add(number)

        variant = Variant.objects.create(
            number=number,
            exam_type=vdata["exam_type"],
            max_attempts=vdata.get("max_attempts", 3),
            is_active=False,
        )
        result["variants_created"] += 1

        for task_ref in vdata.get("tasks", []):
            t_folder = f"{v_folder}/{task_ref['folder']}"
            tdata = json.loads(zf.read(f"{t_folder}/task.json"))

            task = Task(
                variant=variant,
                number=tdata["number"],
                text=tdata.get("text", ""),
                correct_answer=tdata.get("correct_answer", ""),
                source=tdata.get("source", "manual"),
                points=tdata.get("points", 1),
                manual_grading=tdata.get("manual_grading", False),
                no_student_input=tdata.get("no_student_input", False),
                shared_context=tdata.get("shared_context", ""),
            )

            # основное изображение
            img_rel = tdata.get("image")
            if img_rel:
                img_bytes = zf.read(f"{t_folder}/{img_rel}")
                filename = os.path.basename(img_rel)
                task.image.save(filename, ContentFile(img_bytes), save=False)

            # изображение общего условия
            shared_rel = tdata.get("shared_context_image")
            if shared_rel:
                shared_bytes = zf.read(f"{t_folder}/{shared_rel}")
                filename = os.path.basename(shared_rel)
                task.shared_context_image.save(filename, ContentFile(shared_bytes), save=False)

            task.save()
            result["tasks_created"] += 1

            # дополнительные изображения
            for order, extra_rel in enumerate(tdata.get("extra_images", []), start=0):
                extra_bytes = zf.read(f"{t_folder}/{extra_rel}")
                filename = os.path.basename(extra_rel)
                ti = TaskImage(task=task, order=order)
                ti.image.save(filename, ContentFile(extra_bytes), save=False)
                ti.save()
