"""Чистые функции синка: разбор дат, нормализация строки. БД и Google не нужны."""
from src.sync import _parse_date, _normalize_row, _to_float


def test_parse_date_dotted():
    assert _parse_date('17.06.2026') == '2026-06-17'
    assert _parse_date('01.01.2025') == '2025-01-01'


def test_parse_date_iso_accepted():
    assert _parse_date('2026-06-17') == '2026-06-17'


def test_parse_date_empty_and_garbage():
    assert _parse_date('') is None
    assert _parse_date(None) is None
    assert _parse_date('abc') is None
    assert _parse_date('2026-13-40') is None


def test_to_float_empty():
    assert _to_float('') in (None, 0, 0.0)


def _row(**kw):
    base = {'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': 'ПС1', 'ЭЛЕМЕНТ': 'СТ1', 'КОЛ-ВО': '8', 'МАССА ЕД. (кг)': '1.47',
            'МАССА ВСЕХ (кг)': '11.76', 'СУММА К ОПЛАТЕ': '5.99', 'ИСПОЛНИТЕЛЬ': 'ГАНЖА Иван',
            'ДАТА ПЛАН': '17.06.2026', 'ПРИОРИТЕТ': '1', 'ОБЯЗАТЕЛЬНАЯ': 'ДА', 'СТАТУС': 'ПЛАН',
            'КОММЕНТАРИЙ': '', 'ДАТА ФАКТ': '', 'ССЫЛКА НА ЧЕРТЁЖ': 'http://example.com'}
    base.update(kw)
    return base


def test_normalize_row_full():
    r = _normalize_row(_row(), 'ПЛАЗМА', 1, 'Тест', 'file123')
    assert (r['position'], r['element'], r['quantity']) == ('ПС1', 'СТ1', 8.0)
    assert r['mandatory'] is True
    assert r['date_plan'] == '2026-06-17' and r['date_fact'] is None
    assert r['comment'] is None and r['drawing_link'] == 'http://example.com'


def test_normalize_row_optional():
    r = _normalize_row(_row(**{'ЭЛЕМЕНТ': '', 'КОЛ-ВО': '', 'ИСПОЛНИТЕЛЬ': '', 'ОБЯЗАТЕЛЬНАЯ': 'НЕТ'}),
                       'СВАРКА', 2, 'П2', 'file456')
    assert r['mandatory'] is False and r['quantity'] is None and r['executor'] is None
