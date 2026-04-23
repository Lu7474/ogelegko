from datetime import timedelta

from django import template
from django.utils import timezone

register = template.Library()

_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


@register.filter
def human_date(dt):
    """Converts datetime to Сегодня / Вчера / 14 марта."""
    if not dt:
        return ""
    today = timezone.localdate()
    dt_date = timezone.localtime(dt).date()
    if dt_date == today:
        return "Сегодня"
    if dt_date == today - timedelta(days=1):
        return "Вчера"
    return f"{dt_date.day} {_MONTHS[dt_date.month]}"


@register.filter
def get_answer(answers_dict, task_id):
    """Получить объект Answer из словаря {task_id: Answer} по task_id."""
    return answers_dict.get(task_id)


@register.filter
def get_points_range(max_points):
    """Возвращает список [0, 1, ..., max_points] для шаблона выставления баллов."""
    try:
        return range(int(max_points) + 1)
    except (ValueError, TypeError):
        return range(2)
