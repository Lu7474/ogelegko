# Admin/Teacher UI Redesign Brief

> **Branch:** feature/ui-redesign (продолжение — вторая волна)
> **Scope:** Только admin/teacher-facing экраны. Student-facing redesign завершён и трогать его не нужно.
> **Tone:** Тёплый академический стиль — продолжение уже установленной дизайн-системы.
> **Status:** ✅ РЕАЛИЗОВАН (апрель 2026). Все High и Medium приоритеты выполнены.

---

## 1. Текущее состояние Admin UX

### Что уже хорошо (не трогать)
| Компонент | Почему оставить |
|---|---|
| `base_admin.html` — navbar | Тёмный (#0F0B08), Lucide иконки, AJAX-уведомления, бургер на мобильных — уже переработан |
| `catalog_list.html` | Боковая навигация по номерам, карточки заданий, фильтры, bulk-выбор — хорошо структурировано |
| `attempt_detail.html` — AJAX-grading | Live обновление балла/оценки без перезагрузки, multi-point / single-point логика работает чисто |
| CSS design system | Переменные (`--bg`, `--primary`, `--surface`, `--border`), типографика (PT Serif/PT Sans), радиусы, тени — консистентны |
| Modal overlays | Backdrop-blur, `max-height: 90vh`, overflow-y scroll — всё норм |
| Empty states | Есть в `students.html`, `variants.html`, `catalog_list.html` — паттерн установлен |
| Login page | Простой, функциональный, не нужно трогать |

### Что выглядит как старый интерфейс

1. **Dashboard** — голые `<h2>` без разделителей, таблицы без визуальной иерархии, stat-карточки маленькие и однотипные, экспортные кнопки свалены в actions-bar
2. **Classes list** — inline action-кнопки (4 штуки в строке), нет empty state (просто текст "Нет классов"), кнопка добавления с `margin-bottom:20px` в теге
3. **Students list** — фильтры — голые `<select>` без обёртки, переключатель "Только с незачтёнными" реализован как ссылка вместо toggle
4. **Variants list** — 6 inline-кнопок в строке таблицы, bulk bar перегружен 5 отдельными `<form>` тегами параллельно
5. **Student/Class stats** — stat-карточки без иконок и цветов, Chart.js с hardcoded `#3498db`, инлайн-стили `background:#fff5f5` прямо в `{% if %}`
6. **Все формы** — нет helper text, нет visual feedback на сохранение, variant_form без section headers для блоков заданий

---

## 2. Дизайн-направление для Admin

Цель — **не новая система**, а подтягивание admin до уровня student-facing redesign, сохраняя профессиональный рабочий тон.

### Принципы

**Тёплая, профессиональная утилитарность**
- Фон `--bg` (#F4EFE6), карточки `--surface` (#FDFAF4) — те же, что в student UI
- Акцентный синий `--primary` (#2B3F8C) для CTА
- Без яркого маркетингового оформления — это рабочий инструмент учителя

**Иерархия через пространство, не декор**
- `<h1>` страницы + breadcrumb где нужно
- Достаточные `gap` и `padding` чтобы блоки не сливались
- Разделители — тонкие линии `var(--border)`, не заголовки секций

**Actions — компактно и явно**
- Первичное действие — выраженная кнопка `btn-primary`
- Деструктивные (удалить, архивировать) — отдельно, либо в dropdown
- На мобильных — actions collapse в "..." dropdown

**Таблицы — читаемые, не просто сетки**
- Hover-подсветка уже есть, нужно добавить row-level status indicators (левый цветной бордер или badge)
- Мобильный scrollable wrapper вместо ломающегося layout

**Stat-карточки — информативные**
- Иконка + число + label (уже есть на dashboard, но на stats страницах нет)
- Цветной акцент (синий/зелёный/жёлтый/фиолетовый) как на dashboard.html уже сделано для 4 глобальных карточек — распространить на student_stats / class_stats

---

## 3. Приоритизация экранов

### High Priority — ✅ ВЫПОЛНЕНО

| # | Экран | Статус |
|---|---|---|
| H1 | Dashboard | ✅ Реализован |
| H2 | Student Stats | ✅ Реализован |
| H3 | Classes list | ✅ Реализован |
| H4 | Variants list | ✅ Реализован |

### Medium Priority — ✅ ВЫПОЛНЕНО

| # | Экран | Статус |
|---|---|---|
| M1 | Students list | ✅ Реализован |
| M2 | Attempt detail | ✅ Реализован |
| M3 | Class Stats | ✅ Реализован |
| M4 | Формы (class, student, variant) | ✅ class_form + student_form реализованы |

### Дополнительно выполнено (не было в приоритетах)

| Экран | Что сделано |
|---|---|
| `variant_stats.html` | Icons, perf badges, empty state, прогресс-бары |
| `bulk_grade.html` | Empty state, table-responsive, убран `.card` → `.table-responsive` |

### Low Priority / Phase 2

| # | Экран | Причина |
|---|---|---|
| L1 | Catalog import flows | Уже функциональны, комплексная логика |
| L2 | Variant form (сложный редактор) | Уже переработан, риск сломать динамику |
| L3 | Login | Функционален, используется редко |
| L4 | Catalog list | Уже переработан хорошо |

---

## 4. Экраны — детальный разбор

---

### H1. Dashboard ✅

**Acceptance criteria**
- [x] 4 stat-карточки одинаковой высоты, с иконкой, числом, и цветовым акцентом
- [x] Warning о проверке использует CSS-класс, не inline стили (`.info-box.info-box-warning`)
- [x] Таблица последних попыток ограничена 10 строками (в view, `[:10]`)
- [x] Страница не ломается на 375px (table-responsive)
- [x] Экспорт кнопки в отдельном `.export-group` блоке с лейблом
- [x] Section separators `<hr class="section-sep">` перед `<h2>`

---

### H2. Student Stats ✅

**Acceptance criteria**
- [x] Stat-карточки имеют иконки (`percent`, `clipboard-list`) и цвета (`stat-card-blue`, `stat-card-green`)
- [x] Ни одного `style="background:#...` или `style="color:#..."` в шаблоне
- [x] Empty state когда нет попыток — `.empty-state` с иконкой
- [x] Graph color из design system (`#2B3F8C`)
- [x] Строки <50% — `.row-weak` CSS-класс
- [x] "Задания для повторения" — `.info-box.info-box-warning`
- [x] Perf badges вместо inline span

---

### H3. Classes List ✅

**Acceptance criteria**
- [x] Empty state есть — иконка `school` + текст + кнопка добавить
- [x] Кнопка "Добавить класс" в `actions-bar`, без inline стилей
- [x] Страница не ломается на 375px (table-responsive)

---

### H4. Variants List ✅

**Acceptance criteria**
- [x] `--bg-card` переменная исправлена → `--card-bg`
- [x] Таблица обёрнута в `<div class="table-responsive">`
- [x] Bulk bar визуально отличим от основного контента
- [x] Empty state с Lucide иконкой (был эмодзи 📋)

---

### M1. Students List ✅

**Acceptance criteria**
- [x] Есть визуальный индикатор что фильтр "с незачтёнными" активен (`.btn-active` класс)
- [x] Фильтр имеет `<label for="filter-class">Класс:</label>`
- [x] Кнопки в actions-bar с Lucide иконками

---

### M2. Attempt Detail ✅

**Acceptance criteria**
- [x] Шапка использует `.attempt-meta` CSS-класс вместо `.card` с inline стилями
- [x] `.attempt-meta-label` класс для лейблов
- [x] Ни одного hardcoded hex-цвета в шаблоне — используются `.status-pending`, `.status-correct`, `.status-wrong`
- [x] Pending warning использует `.status-inline.status-pending`
- [x] Table-responsive обёртка

---

### M3. Class Stats ✅

**Acceptance criteria**
- [x] 3 stat-карточки с иконками и цветами (`users`, `clipboard-list`, `percent`)
- [x] Empty state для "нет попыток"
- [x] Строки ожидания проверки используют `.status-inline.status-pending`
- [x] Perf badges вместо inline цветов
- [x] Описание для bulk-grade секции

---

### M4. Формы ✅

**Acceptance criteria**
- [x] Django success-messages видны после сохранения (было, теперь без inline стилей)
- [x] Кнопка "Отмена" есть на каждой форме (было и осталось)
- [x] `form-help` helper text на полях (`exam_type` в class_form, `password` в student_form)

---

## 5. Навигация ✅

**Acceptance criteria**
- [x] Активный раздел визуально выделен в navbar (`.active` класс + CSS border-bottom)

---

## 7. Списки / Таблицы ✅

- [x] `.table-responsive` wrapper добавлен во все list-шаблоны
- [x] `.row-inactive` оставлен без изменений (работает)

---

## 8. Карточки статистики ✅

- [x] `student_stats.html` — иконки и цвета добавлены
- [x] `class_stats.html` — иконки и цвета добавлены
- [x] `variant_stats.html` — иконки и цвета добавлены

---

## 12. Empty States ✅

| Экран | Статус |
|---|---|
| classes.html | ✅ Добавлен (иконка `school`) |
| student_stats.html | ✅ Добавлен (иконка `clipboard-list`) |
| class_stats.html | ✅ Добавлен (иконка `bar-chart-2`) |
| variant_stats.html | ✅ Добавлен (иконка `bar-chart-2`) |
| bulk_grade.html | ✅ Обновлён с Lucide иконкой |
| variants.html | ✅ Было emoji, заменён на Lucide иконку |

---

## 13. Mobile Behavior ✅

- [x] `.table-responsive` wrapper добавлен везде
- [x] `.stats-summary` на ≤600px — `grid-template-columns: repeat(2, 1fr)` в CSS
- [x] `.actions-bar` flex-wrap уже был в CSS глобально

---

## 14. What Not To Redesign

Следующее **не трогали** (как и планировалось):

| Компонент | Статус |
|---|---|
| `base_admin.html` navbar | ✅ Не тронут (только active-link и messages) |
| `catalog_list.html` | ✅ Не тронут |
| AJAX-grading в `attempt_detail.html` | ✅ Логика не изменена |
| CSS переменные в `:root` | ✅ Не изменены |
| Modal overlays | ✅ Не тронуты |
| Login page | ✅ Не тронут |
| Variant form editor | ✅ Не тронут |
| Catalog import flows | ✅ Не тронуты (Phase 2) |

---

## 15. Quick Wins — ✅ ВСЕ ВЫПОЛНЕНЫ

| Win | Статус |
|---|---|
| Исправить `--bg-card` → `--card-bg` | ✅ |
| Добавить active-класс на nav-link | ✅ |
| Empty state для classes | ✅ |
| `.table-responsive` обёртка везде | ✅ |
| Иконки на stat-карточки в student_stats | ✅ |
| Empty state для student_stats | ✅ |
| Chart.js цвет из переменной | ✅ |
| Убрать inline стили из attempt_detail шапки | ✅ |
| Django messages без inline стилей | ✅ |
| CSS классы для статусов (pending/correct/wrong) | ✅ (были, теперь используются везде) |

---

## 16. Implementation Order — ВЫПОЛНЕН

### ✅ Этап 0 — Quick Wins
- CSS: `table-responsive`, `form-help`, `btn-active`, `alert-success/error`, `row-weak`, `info-box-warning`, `attempt-meta`, `section-sep`, `export-group`, mobile stats grid, `.card` utility
- `base_admin.html`: active nav-link logic, messages без inline стилей
- `dashboard.html`: info-box-warning, export-group, section-sep, table-responsive

### ✅ Этап 1 — Dashboard & Stats
- `student_stats.html`: иконки, пустое состояние, chart color, perf badges, row-weak, info-box-warning
- `class_stats.html`: те же изменения + описание bulk-grade

### ✅ Этап 2 — Lists polish
- `classes.html`: empty state, actions-bar
- `students.html`: filter label, btn-active, icons
- `variants.html`: `--bg-card` fix, table-responsive, Lucide empty state

### ✅ Этап 3 — Forms & Detail
- `attempt_detail.html`: `.attempt-meta`, `.attempt-meta-label`, status CSS classes
- `class_form.html`: form-help на exam_type
- `student_form.html`: form-help на password

### ✅ Этап 4 — Дополнительно
- `variant_stats.html`: полный рефактор (иконки, perf badges, progress bars, empty state)
- `bulk_grade.html`: empty state с Lucide, table-responsive

### Phase 2 (после запуска — не реализовывать без необходимости)
- Dropdown-меню для actions в таблицах на мобильных
- Улучшенные catalog import flows (progress bar, error states)
- Card-view для таблиц на мобильных

---

## Коммиты (wave 2)

| Хэш | Сообщение |
|---|---|
| `4a86723` | `feat(admin): quick wins — CSS utilities, active nav-link, dashboard polish` |
| `a9e3834` | `feat(admin): stats pages — icons on cards, empty states, perf badges, chart color` |
| `b9b8c0e` | `feat(admin): lists polish — empty states, table-responsive, filter label, bulk bar fix` |
| `12f1588` | `feat(admin): attempt detail — CSS classes replace inline styles; forms — helper text` |
| `ce75520` | `feat(admin): variant stats and bulk grade — icons, perf badges, empty states, progress bars` |
| `8666da6` | `fix(admin): add .card utility class, colored progress bars for analytics` |
