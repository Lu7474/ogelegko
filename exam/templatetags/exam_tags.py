from django import template

register = template.Library()


@register.filter
def get_answer(answers_dict, task_id):
    """Получить объект Answer из словаря {task_id: Answer} по task_id."""
    return answers_dict.get(task_id)
