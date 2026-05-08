from django.test import TestCase

from ..parser import _strip_measurement_unit
from ..utils import check_answer, get_grade, normalize_answer, normalize_full_name


class NormalizeAnswerTests(TestCase):
    def test_integer(self):
        self.assertEqual(normalize_answer("42"), "42")

    def test_decimal_comma(self):
        self.assertEqual(normalize_answer("3,5"), "3.5")

    def test_fraction(self):
        self.assertEqual(normalize_answer("1/2"), "0.5")

    def test_strip_spaces(self):
        self.assertEqual(normalize_answer("  7  "), "7")

    def test_empty(self):
        self.assertEqual(normalize_answer(""), "")

    def test_text(self):
        self.assertEqual(normalize_answer("abc"), "abc")

    def test_unicode_minus(self):
        self.assertEqual(normalize_answer("−" + "5"), "-5")

    def test_endash_minus(self):
        self.assertEqual(normalize_answer("–" + "3"), "-3")

    def test_unicode_minus_in_answer_match(self):
        from ..utils import check_answer

        self.assertTrue(check_answer("-5", "−" + "5"))
        self.assertTrue(check_answer("−" + "5", "-5"))


class CheckAnswerTests(TestCase):
    def test_exact(self):
        self.assertTrue(check_answer("42", "42"))

    def test_comma_vs_dot(self):
        self.assertTrue(check_answer("3,5", "3.5"))

    def test_wrong(self):
        self.assertFalse(check_answer("41", "42"))

    def test_fraction_match(self):
        self.assertTrue(check_answer("1/4", "0.25"))

    def test_empty_answer_is_wrong(self):
        self.assertFalse(check_answer("", "42"))

    def test_pipe_alternatives_first(self):
        self.assertTrue(check_answer("234", "234|243|324"))

    def test_pipe_alternatives_last(self):
        self.assertTrue(check_answer("324", "234|243|324"))

    def test_pipe_alternatives_wrong(self):
        self.assertFalse(check_answer("999", "234|243|324"))

    def test_pi_without_symbol(self):
        self.assertTrue(check_answer("27", "27π"))

    def test_pi_with_symbol(self):
        self.assertTrue(check_answer("27π", "27π"))

    def test_pi_keyword(self):
        self.assertTrue(check_answer("27pi", "27π"))

    def test_pi_alone_not_simplified(self):
        self.assertFalse(check_answer("1", "π"))

    def test_pi_wrong_coeff(self):
        self.assertFalse(check_answer("28", "27π"))


class StripUnitTests(TestCase):
    def test_mm(self):
        self.assertEqual(_strip_measurement_unit("0.4 мм"), "0.4")

    def test_km_h(self):
        self.assertEqual(_strip_measurement_unit("60 км/ч"), "60")

    def test_percent(self):
        self.assertEqual(_strip_measurement_unit("15%"), "15")

    def test_rub(self):
        self.assertEqual(_strip_measurement_unit("1200 руб"), "1200")

    def test_no_unit(self):
        self.assertEqual(_strip_measurement_unit("42"), "42")

    def test_text_answer(self):
        self.assertEqual(_strip_measurement_unit("нет"), "нет")

    def test_decimal_with_unit(self):
        self.assertEqual(_strip_measurement_unit("3,5 кг"), "3,5")


class GradeTests(TestCase):
    def test_oge_grade_5(self):
        self.assertEqual(get_grade("oge", 25), "5")

    def test_oge_grade_2(self):
        self.assertEqual(get_grade("oge", 3), "2")

    def test_ege_profile(self):
        self.assertEqual(get_grade("ege_profile", 15), "72")

    def test_ege_base_grade_3(self):
        self.assertEqual(get_grade("ege_base", 8), "3")


class NormalizeFullNameTests(TestCase):
    def test_lowercase(self):
        self.assertEqual(normalize_full_name("иванов иван иванович"), "Иванов Иван Иванович")

    def test_uppercase(self):
        self.assertEqual(normalize_full_name("ПЕТРОВ ПЁТР"), "Петров Пётр")

    def test_extra_spaces(self):
        self.assertEqual(normalize_full_name("  сидоров   сидор  "), "Сидоров Сидор")

    def test_already_normalized(self):
        self.assertEqual(normalize_full_name("Козлов Иван"), "Козлов Иван")

    def test_single_word(self):
        self.assertEqual(normalize_full_name("иван"), "Иван")

    def test_empty(self):
        self.assertEqual(normalize_full_name(""), "")

    def test_tabs_and_newlines_collapsed(self):
        self.assertEqual(normalize_full_name("иванов\tиван\nиванович"), "Иванов Иван Иванович")
