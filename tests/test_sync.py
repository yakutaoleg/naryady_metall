import pytest
import sys
sys.path.insert(0, '/root/naryady/test')

from src.sync import _parse_date, _normalize_row, run
from src import db

def test_parse_date_valid():
    assert _parse_date('17.06.2026') == '2026-06-17'
    assert _parse_date('01.01.2025') == '2025-01-01'

def test_parse_date_invalid():
    assert _parse_date('') is None
    assert _parse_date(None) is None
    assert _parse_date('2026-06-17') is None
    assert _parse_date('abc') is None

def test_normalize_row_full():
    row = {
        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': 'ПС1',
        'ЭЛЕМЕНТ': 'СТ1',
        'КОЛ-ВО': '8',
        'МАССА ЕД. (кг)': '1.47',
        'МАССА ВСЕХ (кг)': '11.76',
        'СУММА К ОПЛАТЕ': '5.99',
        'ИСПОЛНИТЕЛЬ': 'ГАНЖА Иван',
        'ДАТА ПЛАН': '17.06.2026',
        'ПРИОРИТЕТ': '1',
        'ОБЯЗАТЕЛЬНАЯ': 'ДА',
        'СТАТУС': 'ПЛАН',
        'КОММЕНТАРИЙ': '',
        'ДАТА ФАКТ': '',
        'ССЫЛКА НА ЧЕРТЁЖ': 'http://example.com',
    }
    r = _normalize_row(row, 'ПЛАЗМА', 1, 'Тест проект', 'file123')
    assert r['position'] == 'ПС1'
    assert r['element'] == 'СТ1'
    assert r['quantity'] == 8.0
    assert r['mandatory'] is True
    assert r['date_plan'] == '2026-06-17'
    assert r['date_fact'] is None
    assert r['comment'] is None
    assert r['drawing_link'] == 'http://example.com'

def test_normalize_row_optional():
    row = {
        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': 'ПС2',
        'ЭЛЕМЕНТ': '',
        'КОЛ-ВО': '',
        'МАССА ЕД. (кг)': '',
        'МАССА ВСЕХ (кг)': '',
        'СУММА К ОПЛАТЕ': '',
        'ИСПОЛНИТЕЛЬ': '',
        'ДАТА ПЛАН': '',
        'ПРИОРИТЕТ': '',
        'ОБЯЗАТЕЛЬНАЯ': 'НЕТ',
        'СТАТУС': 'ПЛАН',
        'КОММЕНТАРИЙ': '',
        'ДАТА ФАКТ': '',
        'ССЫЛКА НА ЧЕРТЁЖ': '',
    }
    r = _normalize_row(row, 'СВАРКА', 2, 'Проект2', 'file456')
    assert r['mandatory'] is False
    assert r['quantity'] is None
    assert r['executor'] is None

def test_sync_run():
    run()
    rows = db.fetchall('SELECT * FROM work_orders LIMIT 10', [])
    assert len(rows) >= 0  # просто проверяем что не падает
