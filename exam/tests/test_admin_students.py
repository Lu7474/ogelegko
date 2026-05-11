import io

from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import ExamType, SchoolClass, Student


def _admin_client(username="adm_stu"):
    client = Client()
    User.objects.create_user(username, password="pass", is_staff=True)
    client.login(username=username, password="pass")
    return client


def _make_class(name="9А", exam_type=ExamType.OGE):
    return SchoolClass.objects.create(name=name, exam_type=exam_type)


class StudentAddTests(TestCase):
    def setUp(self):
        self.client = _admin_client()
        self.cls = _make_class()

    def test_add_student_creates_and_redirects(self):
        resp = self.client.post(
            "/admin/students/add/",
            {"full_name": "Иванов Иван", "password": "pass1", "school_class": self.cls.id},
        )
        self.assertRedirects(resp, "/admin/students/", fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(full_name="Иванов Иван").exists())

    def test_add_student_empty_name_shows_error(self):
        resp = self.client.post(
            "/admin/students/add/",
            {"full_name": "", "password": "pass1", "school_class": self.cls.id},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите ФИО")

    def test_add_student_no_password_shows_error(self):
        resp = self.client.post(
            "/admin/students/add/",
            {"full_name": "Петров Пётр", "password": "", "school_class": self.cls.id},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите пароль")

    def test_add_student_no_class_shows_error(self):
        resp = self.client.post(
            "/admin/students/add/",
            {"full_name": "Сидоров Сидор", "password": "pass1", "school_class": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Выберите класс")

    def test_add_duplicate_student_shows_error(self):
        s = Student(full_name="Дубль Дубль", school_class=self.cls)
        s.set_password("x")
        s.save()
        resp = self.client.post(
            "/admin/students/add/",
            {"full_name": "Дубль Дубль", "password": "pass1", "school_class": self.cls.id},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже существует")

    def test_get_add_returns_200(self):
        resp = self.client.get("/admin/students/add/")
        self.assertEqual(resp.status_code, 200)


class StudentEditTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_stu2")
        self.cls = _make_class(name="9Б")
        self.student = Student(full_name="Редактов Редакт", school_class=self.cls)
        self.student.set_password("pass")
        self.student.save()

    def test_edit_student_updates_name(self):
        resp = self.client.post(
            f"/admin/students/{self.student.id}/edit/",
            {"full_name": "Новый Новый", "school_class": self.cls.id, "password": ""},
        )
        self.assertRedirects(resp, "/admin/students/", fetch_redirect_response=False)
        self.student.refresh_from_db()
        self.assertEqual(self.student.full_name, "Новый Новый")

    def test_edit_student_with_new_password(self):
        resp = self.client.post(
            f"/admin/students/{self.student.id}/edit/",
            {"full_name": "Редактов Редакт", "school_class": self.cls.id, "password": "newpass"},
        )
        self.assertRedirects(resp, "/admin/students/", fetch_redirect_response=False)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("newpass"))

    def test_edit_student_empty_name_shows_error(self):
        resp = self.client.post(
            f"/admin/students/{self.student.id}/edit/",
            {"full_name": "", "school_class": self.cls.id, "password": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите ФИО")

    def test_get_edit_returns_200(self):
        resp = self.client.get(f"/admin/students/{self.student.id}/edit/")
        self.assertEqual(resp.status_code, 200)


class StudentDeleteTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_stu3")
        self.cls = _make_class(name="9В")
        self.student = Student(full_name="Удалёнов Удалён", school_class=self.cls)
        self.student.set_password("pass")
        self.student.save()

    def test_delete_removes_student(self):
        resp = self.client.post(f"/admin/students/{self.student.id}/delete/")
        self.assertRedirects(resp, "/admin/students/", fetch_redirect_response=False)
        self.assertFalse(Student.objects.filter(id=self.student.id).exists())

    def test_delete_get_not_allowed(self):
        resp = self.client.get(f"/admin/students/{self.student.id}/delete/")
        self.assertEqual(resp.status_code, 405)


class StudentImportTests(TestCase):
    def setUp(self):
        self.client = _admin_client(username="adm_stu4")
        self.cls = _make_class(name="9Г")

    def _make_xlsx(self, rows):
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ФИО", "Пароль", "Класс"])
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = "students.xlsx"
        return buf

    def test_import_creates_students(self):
        xlsx = self._make_xlsx([["Импортов Импорт", "pass1", "9Г"]])
        resp = self.client.post("/admin/students/import/", {"file": xlsx})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Student.objects.filter(full_name="Импортов Импорт").exists())
        self.assertContains(resp, "Успешно добавлено")

    def test_import_unknown_class_shows_error(self):
        xlsx = self._make_xlsx([["Ошибков Ошибка", "pass1", "Несуществующий"]])
        resp = self.client.post("/admin/students/import/", {"file": xlsx})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "не найден")

    def test_import_non_xlsx_shows_error(self):
        bad = io.BytesIO(b"not excel")
        bad.name = "students.csv"
        resp = self.client.post("/admin/students/import/", {"file": bad})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "xlsx")

    def test_import_duplicate_skips_with_error(self):
        s = Student(full_name="Дубль Дубль", school_class=self.cls)
        s.set_password("x")
        s.save()
        xlsx = self._make_xlsx([["Дубль Дубль", "pass1", "9Г"]])
        resp = self.client.post("/admin/students/import/", {"file": xlsx})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже существует")
        self.assertEqual(Student.objects.filter(full_name="Дубль Дубль").count(), 1)

    def test_get_import_returns_200(self):
        resp = self.client.get("/admin/students/import/")
        self.assertEqual(resp.status_code, 200)
