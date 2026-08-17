"""
sheet_standards.py — единый источник правил визуального оформления листов.

Импортируется из sheets.py и project_wizard.py.
Добавлять новые правила сюда — они автоматически применяются везде.
"""

# ── Ширины колонок (px). None = не трогаем. ──────────────────────────────
COL_WIDTHS = {
    "ПОЗ. СОГЛАСНО ЧЕРТЕЖА":    175,
    "Марка":                     175,
    "ЭЛЕМЕНТ":                   100,
    "Поверхность\nЭлемент (м²)": 100,
    "КОЛ-ВО":                    65,
    "Кол-во":                    65,
    "МАССА ЕД. (кг)":            65,
    "Покраска\nза (м²)":         80,
    "Грунтовка\nза (м²)":        80,
    "МАССА ВСЕХ (кг)":           65,
    "СУММА К ОПЛАТЕ":            95,
    "ИСПОЛНИТЕЛЬ":               170,
    "БЛОК":                      160,
    "ДАТА ПЛАН":                 85,
    "ОБЯЗАТЕЛЬНАЯ":              120,
    "СТАТУС":                    120,
    "ВЫПОЛНЕНО":                 95,
    "КОММЕНТАРИЙ":               110,
    "ДАТА ФАКТ":                 80,
    "ССЫЛКА НА ЧЕРТЁЖ":          500,
    "КОЛ-ВО ОТВЕРСТИЙ":         70,
    "ROW_ID":                    None,  # скрытая
}

HEADER_ROW_HEIGHT = 32  # высота строки заголовков (строка 2)
FROZEN_ROWS = 2          # закрепить строки 1-2 (заголовок всегда виден при прокрутке)

# ── Цвета статусов ────────────────────────────────────────────────────────
# ВАЖНО: цвет применяется ТОЛЬКО к ячейке колонки СТАТУС.
# Фон всей строки данных — белый (#FFFFFF). Фон строки ИТОГО — #EFEFEF.
def _c(hex_str):
    h = hex_str.lstrip('#')
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}

STATUS_COLORS = {
    'ПЛАН':      {'bg': _c('FFF9C4'), 'bold': True},
    'ВЫПОЛНЕНО': {'bg': _c('E8F5E9'), 'bold': True},
    'ЧАСТИЧНО':  {'bg': _c('FFE0B2'), 'bold': True},
    'БЛОК':      {'bg': _c('FFEBEE'), 'bold': True},
}

# ── Колонки с датами → calendar picker ───────────────────────────────────
DATE_COLUMNS = {'ДАТА ПЛАН', 'ДАТА ФАКТ'}

# ── Правила дропдаунов ────────────────────────────────────────────────────
# Применяются в add_data_validations (project_wizard.py) по реальным индексам из _col_map.
DROPDOWN_RULES = {
    'СТАТУС': {
        'type': 'ONE_OF_LIST',
        'values': ['ПЛАН', 'ВЫПОЛНЕНО', 'ЧАСТИЧНО', 'БЛОК'],
        'showCustomUi': True,
        'strict': True,
    },
    'ОБЯЗАТЕЛЬНАЯ': {
        'type': 'ONE_OF_LIST',
        'values': ['ДА', 'НЕТ'],
        'showCustomUi': True,
        'strict': False,
    },
    # ИСПОЛНИТЕЛЬ — ONE_OF_RANGE, строится динамически в project_wizard.py
    # т.к. зависит от индекса специализации (столбец в листе СОТРУДНИКИ)
}

# ── Значения по умолчанию для строк с заполненной позицией ──────────────
# Если в строке есть позиция (ПОЗ. СОГЛАСНО ЧЕРТЕЖА / Марка), то:
# СТАТУС должен быть заполнен (дефолт — ПЛАН)
# ОБЯЗАТЕЛЬНАЯ должна быть заполнена (дефолт — НЕТ)
DEFAULT_ROW_VALUES = {
    'СТАТУС':       'ПЛАН',
    'ОБЯЗАТЕЛЬНАЯ': 'НЕТ',
}

# ── Допустимые цвета фона строк данных (для мониторинга) ────────────────
# Мониторинг сравнивает bg_hex(effectiveFormat.backgroundColor) с этим списком.
# ВАЖНО: Google Sheets возвращает цвет как float 0.0–1.0. При конвертации
# в hex используется round() а не int() — иначе E8F5E9 может прийти как E7F4E9
# из-за погрешности float (разница в 1/255 ≈ 0.004).
ALLOWED_BG_COLORS = {
    'FFFFFF',  # белый — обычные строки данных
    'FFF9C4',  # ПЛАН
    'E8F5E9',  # ВЫПОЛНЕНО
    'FFE0B2',  # ЧАСТИЧНО
    'FFEBEE',  # БЛОК
    'EEEEEE',  # заголовок
    'EFEFEF',  # заголовок / ИТОГО
}

# ── Форматирование ячеек ──────────────────────────────────────────────────
# Все параметры для batchUpdate Sheets API.

HEADER_CELL_FORMAT = {
    'textFormat': {
        'fontFamily': 'Arial',
        'fontSize': 9,
        'bold': True,
    },
    'horizontalAlignment': 'CENTER',
    'verticalAlignment': 'MIDDLE',
    'wrapStrategy': 'WRAP',
    'backgroundColor': _c('EFEFEF'),
}

DATA_CELL_FORMAT = {
    'textFormat': {
        'fontFamily': 'Arial',
        'fontSize': 9,
        'bold': False,
    },
    'horizontalAlignment': 'LEFT',
    'verticalAlignment': 'MIDDLE',
    'wrapStrategy': 'CLIP',
    'backgroundColor': {'red': 1, 'green': 1, 'blue': 1},
}

# Колонки с особым форматом данных (переопределяют DATA_CELL_FORMAT частично)
COLUMN_OVERRIDES = {
    'СТАТУС': {
        'textFormat': {'fontFamily': 'Arial', 'fontSize': 9, 'bold': True},
        'horizontalAlignment': 'CENTER',
    },
    'ОБЯЗАТЕЛЬНАЯ': {
        'horizontalAlignment': 'CENTER',
    },
    'КОЛ-ВО': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'Кол-во': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'МАССА ЕД. (кг)': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'МАССА ВСЕХ (кг)': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'СУММА К ОПЛАТЕ': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'Покраска\nза (м²)': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'Грунтовка\nза (м²)': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'Поверхность\nЭлемент (м²)': {
        'horizontalAlignment': 'RIGHT',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'КОЛ-ВО ОТВЕРСТИЙ': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'ДАТА ПЛАН': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'},
    },
    'ДАТА ФАКТ': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'},
    },
    'ВЫПОЛНЕНО': {
        'horizontalAlignment': 'CENTER',
        'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'},
    },
    'ССЫЛКА НА ЧЕРТЁЖ': {
        'wrapStrategy': 'CLIP',
        'textFormat': {'foregroundColor': {}},
    },
    'БЛОК': {
        'horizontalAlignment': 'LEFT',
        'wrapStrategy': 'WRAP',
    },
}

# ── Формат строки ИТОГО ───────────────────────────────────────────────────
ITOGO_CELL_FORMAT = {
    'textFormat': {'fontFamily': 'Arial', 'fontSize': 9, 'bold': True},
    'horizontalAlignment': 'CENTER',
    'verticalAlignment': 'MIDDLE',
    'wrapStrategy': 'CLIP',
    'backgroundColor': _c('EFEFEF'),
}

# ── Границы ячеек ─────────────────────────────────────────────────────────
# Тонкая сплошная чёрная граница вокруг каждой ячейки (применяется ко всем
# рабочим вкладкам через apply_column_standards / apply_borders).
_BORDER_SOLID_THIN = {
    'style': 'SOLID',
    'color': _c('000000'),
    'width': 1,
}

TABLE_BORDERS = {
    'top':    _BORDER_SOLID_THIN,
    'bottom': _BORDER_SOLID_THIN,
    'left':   _BORDER_SOLID_THIN,
    'right':  _BORDER_SOLID_THIN,
}
