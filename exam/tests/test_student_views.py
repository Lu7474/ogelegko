from django.test import Client, TestCase

from ..models import Answer, Attempt, ExamType, SchoolClass, Student, Task, Variant


def _make_student(name="Иванов Иван", cls=None, password="pass"):
    if cls is None:
        cls, _ = SchoolClass.objects.get_or_create(name="11А", defaults={"exam_type": ExamType.EGE_PROFILE})
    s = Student(full_name=name, school_class=cls)
    s.set_password(password)
    s.save()
    return s


def _login_student(client, student):
    session = client.session
    session["student_id"] = student.id
    session.save()
    student.session_key = client.session.session_key
    student.save(update_fields=["session_key"])


def _make_finished_attempt(student, variant, score=1, max_score=2):
    attempt = Attempt.objects.create(
        student=student,
        variant=variant,
        is_finished=True,
        score=score,
        max_score=max_score,
    )
    return attempt


class ProfileViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.cls = SchoolClass.objects.create(name="11Б", exam_type=ExamType.EGE_PROFILE)
        self.student = _make_student("Профилев Профиль", cls=self.cls)
        self.variant = Variant.objects.create(number="pv1", exam_type=ExamType.EGE_PROFILE)
        _login_student(self.client, self.student)

    def test_profile_empty_returns_200(self):
        resp = self.client.get("/profile/")
        self.assertEqual(resp.status_code, 200)

    def test_profile_with_attempts_shows_stats(self):
        _make_finished_attempt(self.student, self.variant, score=3, max_score=4)
        resp = self.client.get("/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("avg_percentage", resp.context)
        self.assertIsNotNone(resp.context["avg_percentage"])

    def test_profile_unauthenticated_redirects(self):
        self.client.session.flush()
        resp = self.client.get("/profile/")
        self.assertNotEqual(resp.status_code, 200)

    def test_profile_trend_computed_with_three_attempts(self):
        for score in [4, 2, 3]:
            _make_finished_attempt(self.student, self.variant, score=score, max_score=4)
        resp = self.client.get("/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.context["trend"], ("↑", "↓", "→"))


class ViewAttemptTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.cls = SchoolClass.objects.create(name="11В", exam_type=ExamType.EGE_PROFILE)
        self.student = _make_student("Просмотров Просмотр", cls=self.cls)
        self.variant = Variant.objects.create(number="va1", exam_type=ExamType.EGE_PROFILE)
        task = Task.objects.create(variant=self.variant, number=1, text="1+1", correct_answer="2", points=1)
        self.attempt = _make_finished_attempt(self.student, self.variant, score=1, max_score=1)
        Answer.objects.create(
            attempt=self.attempt, task=task, student_answer="2", is_correct=True, awarded_points=1
        )
        _login_student(self.client, self.student)

    def test_view_attempt_returns_200(self):
        resp = self.client.get(f"/attempt/{self.attempt.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_view_attempt_wrong_student_returns_404(self):
        other_cls = SchoolClass.objects.create(name="11Г", exam_type=ExamType.EGE_PROFILE)
        other = _make_student("Чужой Чужой", cls=other_cls)
        _login_student(self.client, other)
        resp = self.client.get(f"/attempt/{self.attempt.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_view_attempt_unfinished_returns_404(self):
        unfinished = Attempt.objects.create(student=self.student, variant=self.variant, is_finished=False)
        resp = self.client.get(f"/attempt/{unfinished.id}/")
        self.assertEqual(resp.status_code, 404)


class ChooseVariantTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.cls = SchoolClass.objects.create(name="11Д", exam_type=ExamType.EGE_PROFILE)
        self.student = _make_student("Выборов Выбор", cls=self.cls)
        self.variant = Variant.objects.create(number="cv1", exam_type=ExamType.EGE_PROFILE, is_active=True)
        Task.objects.create(variant=self.variant, number=1, text="q", correct_answer="a", points=1)
        _login_student(self.client, self.student)

    def test_get_returns_200(self):
        resp = self.client.get("/choose/")
        self.assertEqual(resp.status_code, 200)

    def test_random_redirects_to_exam(self):
        resp = self.client.post("/choose/", {"action": "random"})
        self.assertRedirects(resp, f"/exam/{self.variant.id}/", fetch_redirect_response=False)

    def test_random_no_variants_shows_error(self):
        self.variant.is_active = False
        self.variant.save()
        resp = self.client.post("/choose/", {"action": "random"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Нет доступных вариантов")

    def test_by_id_found_redirects(self):
        resp = self.client.post("/choose/", {"action": "by_id", "variant_number": "cv1"})
        self.assertRedirects(resp, f"/exam/{self.variant.id}/", fetch_redirect_response=False)

    def test_by_id_not_found_shows_error(self):
        resp = self.client.post("/choose/", {"action": "by_id", "variant_number": "nope"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "не найден")

    def test_by_id_empty_shows_error(self):
        resp = self.client.post("/choose/", {"action": "by_id", "variant_number": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Введите ID")
