import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from .models import Answer, Attempt, ExamType, SchoolClass, Student, Task, Variant


class ExamFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(name="9Б", exam_type=ExamType.OGE)
        self.student = Student(
            full_name="Экзаменов Экзамен",
            school_class=self.school_class,
        )
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(
            number="test001",
            exam_type=ExamType.OGE,
            max_attempts=2,
        )
        self.task1 = Task.objects.create(
            variant=self.variant,
            number=1,
            text="2+2=?",
            correct_answer="4",
            points=1,
        )
        self.task2 = Task.objects.create(
            variant=self.variant,
            number=2,
            text="3+3=?",
            correct_answer="6",
            points=1,
        )

        self.client.post(
            "/login/",
            {
                "full_name": "Экзаменов Экзамен",
                "password": "pass",
            },
        )

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
        a2.student_answer = "5"
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

        resp = self.client.get(f"/exam/{self.variant.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Attempt.objects.filter(student=self.student, is_finished=True).count(), 2)
        self.assertEqual(Attempt.objects.filter(student=self.student, is_finished=False).count(), 0)


class RetryMistakesTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(name="9В", exam_type=ExamType.OGE)
        self.student = Student(full_name="Ошибков Ошибка", school_class=self.school_class)
        self.student.set_password("pass")
        self.student.save()

        self.variant = Variant.objects.create(number="retry_base", exam_type=ExamType.OGE)
        self.task = Task.objects.create(
            variant=self.variant, number=1, text="1+1=?", correct_answer="2", points=1
        )

        self.client.post("/login/", {"full_name": "Ошибков Ошибка", "password": "pass"})

        self.client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student)
        answer = Answer.objects.get(attempt=attempt, task=self.task)
        answer.student_answer = "99"
        answer.save()
        self.client.post(f"/exam/finish/{attempt.id}/")
        self.attempt = Attempt.objects.get(student=self.student, is_finished=True)

    def test_retry_variant_is_active(self):
        resp = self.client.get(f"/attempt/{self.attempt.id}/retry/")
        self.assertEqual(resp.status_code, 302)

        review_number = f"ошибки_{self.variant.number}_{self.attempt.id}"
        review_variant = Variant.objects.get(number=review_number)
        self.assertTrue(review_variant.is_active)

    def test_retry_start_exam_accessible(self):
        self.client.get(f"/attempt/{self.attempt.id}/retry/")

        review_number = f"ошибки_{self.variant.number}_{self.attempt.id}"
        review_variant = Variant.objects.get(number=review_number)

        resp = self.client.get(f"/exam/{review_variant.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_retry_excluded_from_random(self):
        self.client.get(f"/attempt/{self.attempt.id}/retry/")
        self.variant.is_active = False
        self.variant.save()

        resp = self.client.post("/choose/", {"action": "random"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Нет доступных вариантов")


class ExamTimerTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school_class = SchoolClass.objects.create(name="10Г", exam_type=ExamType.OGE)
        self.student = Student(full_name="Таймеров Тест", school_class=self.school_class)
        self.student.set_password("pass")
        self.student.save()
        self.variant = Variant.objects.create(number="timer_v1", exam_type=ExamType.OGE)
        self.task = Task.objects.create(
            variant=self.variant, number="1", text="1+1=?", correct_answer="2", points=1
        )
        self.client.post("/login/", {"full_name": "Таймеров Тест", "password": "pass"})

    def _expire_attempt(self, attempt):
        Attempt.objects.filter(id=attempt.id).update(started_at=timezone.now() - timedelta(hours=5))
        attempt.refresh_from_db()

    def test_save_answer_returns_403_when_expired(self):
        self.client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student, is_finished=False)
        answer = Answer.objects.get(attempt=attempt, task=self.task)
        self._expire_attempt(attempt)

        resp = self.client.post(
            "/exam/save-answer/",
            json.dumps({"answer_id": answer.id, "value": "2"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(json.loads(resp.content).get("error"), "time_expired")

    def test_start_exam_redirects_to_results_when_expired(self):
        self.client.get(f"/exam/{self.variant.id}/")
        attempt = Attempt.objects.get(student=self.student, is_finished=False)
        self._expire_attempt(attempt)

        resp = self.client.get(f"/exam/{self.variant.id}/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("results", resp.url)
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_finished)
