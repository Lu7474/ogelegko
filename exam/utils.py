import re
from fractions import Fraction


def normalize_full_name(name: str) -> str:
    """Trim, collapse whitespace, title-case each word.

    "иванов   иван иванович" → "Иванов Иван Иванович"
    "ПЕТРОВ ПЁТР"            → "Петров Пётр"
    """
    return " ".join(word.capitalize() for word in name.split())


def normalize_answer(answer: str) -> str:
    """Нормализует ответ для сравнения. Приводит дроби и числа к единому формату."""
    answer = answer.strip()
    # Нормализуем знаки минуса: U+2212 (математический), U+2013 (en-dash) → ASCII дефис
    answer = answer.replace("\u2212", "-").replace("\u2013", "-")
    # Нормализуем π: "pi" после цифры ("27pi") или отдельно ("pi") → символ π
    answer = re.sub(r"(?i)(?<=\d)pi|^pi$", "π", answer)
    answer = answer.replace(",", ".").replace(" ", "")
    # Убираем π из конца числового ответа: "27π" → "27", "-3π" → "-3"
    # (ученик вводит только коэффициент, π — часть единицы измерения ответа)
    answer = re.sub(r"^(-?\d+(?:\.\d+)?)π$", r"\1", answer)
    if not answer:
        return ""

    # Попробуем распарсить как дробь (1/2, 3/4)
    if "/" in answer:
        try:
            frac = Fraction(answer)
            return str(float(frac))
        except (ValueError, ZeroDivisionError):
            pass

    # Попробуем как число
    try:
        num = float(answer)
        # Если целое — убираем .0
        if num == int(num):
            return str(int(num))
        return str(num)
    except ValueError:
        pass

    # Иначе возвращаем как есть (в нижнем регистре)
    return answer.lower()


def check_answer(student_answer: str, correct_answer: str) -> bool:
    """Проверяет правильность ответа с нормализацией.
    Правильный ответ может содержать несколько вариантов через | (например, 234|243|324).
    Если правильный ответ вида Nπ — принимается и ответ без π (ученик не может набрать символ).
    """
    if not student_answer or not student_answer.strip():
        return False
    norm_student = normalize_answer(student_answer)
    alternatives = [normalize_answer(a) for a in correct_answer.split("|")]
    if norm_student in alternatives:
        return True
    # Для ответов вида "Nπ": принимаем коэффициент без π
    # "27π" → принять "27" или "27pi"; "π" (без коэффициента) — не упрощаем
    for alt in alternatives:
        if alt.endswith("π") and len(alt) > 1:
            coeff = normalize_answer(alt[:-1])
            if coeff and norm_student == coeff:
                return True
    return False


# Таблицы перевода баллов (2025/2026)

# ОГЭ математика: первичный балл → оценка
OGE_GRADE_TABLE = {
    range(0, 8): "2",
    range(8, 15): "3",
    range(15, 22): "4",
    range(22, 33): "5",
}

# ЕГЭ профиль: первичный балл → тестовый балл
EGE_PROFILE_TABLE = {
    0: 0,
    1: 6,
    2: 11,
    3: 17,
    4: 22,
    5: 27,
    6: 34,
    7: 40,
    8: 46,
    9: 52,
    10: 58,
    11: 64,
    12: 66,
    13: 68,
    14: 70,
    15: 72,
    16: 74,
    17: 76,
    18: 78,
    19: 80,
    20: 82,
    21: 84,
    22: 86,
    23: 88,
    24: 90,
    25: 92,
    26: 94,
    27: 96,
    28: 98,
    29: 99,
    30: 100,
}

# ЕГЭ база: первичный балл → оценка
EGE_BASE_GRADE_TABLE = {
    range(0, 7): "2",
    range(7, 12): "3",
    range(12, 17): "4",
    range(17, 22): "5",
}


# Задания ОГЭ, по которым считается обязательный минимум 2 балла для оценки ≥ 3
OGE_REQUIRED_SECTION = frozenset(range(15, 20)) | frozenset(range(23, 26))  # 15–19 и 23–25


def get_grade(exam_type: str, primary_score: int) -> str:
    """Возвращает оценку/тестовый балл по первичному баллу."""
    if exam_type == "oge":
        for score_range, grade in OGE_GRADE_TABLE.items():
            if primary_score in score_range:
                return grade
        return "5"

    elif exam_type == "ege_profile":
        if primary_score in EGE_PROFILE_TABLE:
            return str(EGE_PROFILE_TABLE[primary_score])
        # Если больше максимума в таблице
        max_key = max(EGE_PROFILE_TABLE.keys())
        if primary_score >= max_key:
            return "100"
        return "0"

    elif exam_type == "ege_base":
        for score_range, grade in EGE_BASE_GRADE_TABLE.items():
            if primary_score in score_range:
                return grade
        return "5"

    return "—"


def get_grade_display(exam_type: str, grade: str) -> str:
    """Форматирует отображение оценки."""
    if exam_type == "ege_profile":
        return f"{grade} тестовых баллов"
    return f"Оценка: {grade}"


def get_grade_for_attempt(attempt) -> str:
    """Вычисляет оценку с учётом дополнительных критериев ОГЭ.

    ОГЭ: если сумма баллов по заданиям 15–19 и 23–25 меньше 2 — оценка 2
    независимо от общего первичного балла.
    """
    exam_type = attempt.variant.exam_type

    if exam_type == "oge":
        section_score = 0
        for answer in attempt.answers.select_related("task").all():
            try:
                n = int(str(answer.task.number).split(".")[0])
            except (ValueError, TypeError):
                continue
            if n in OGE_REQUIRED_SECTION:
                if answer.awarded_points is not None:
                    section_score += answer.awarded_points
                elif answer.is_correct:
                    section_score += answer.task.points
        if section_score < 2:
            return "2"

    return get_grade(exam_type, attempt.score)
