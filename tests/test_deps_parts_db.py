"""Зависимости с количеством деталей: расчёт на реальных таблицах (изолированный проект __pytest__)
и согласованность четырёх мест, где считается блокировка: sync, bot._deps_*, bot._available_qty_map."""
import pytest
from tests.conftest import PN
from src import deps_parts as dp, sync, bot


@pytest.fixture(autouse=True)
def _no_cache():
    dp._cache.clear()
    yield
    dp._cache.clear()


def _setup(pdb, mk, kp1_done):
    """Плазма: кп1 всего 40 (kp1_done готово). Сборки Б4, Б4б готовы, Б4в ждёт. Каждой нужно по 2 кп1."""
    mk(1, sheet='ПЛАЗМА', element='готовый элемент', qty=kp1_done, status='ВЫПОЛНЕНО', position='кп1')
    mk(2, sheet='ПЛАЗМА', element='готовый элемент', qty=40 - kp1_done, status='ПЛАН', position='кп1')
    mk(1, sheet='СБОРКА', element='Б4', qty=1, status='ВЫПОЛНЕНО')
    mk(2, sheet='СБОРКА', element='Б4б', qty=1, status='ВЫПОЛНЕНО')
    mk(3, sheet='СБОРКА', element='Б4в', qty=1, status='ПЛАН')
    for e in ('Б4', 'Б4б', 'Б4в'):
        pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position) "
                    "VALUES (%s,'СБОРКА',%s,'ПЛАЗМА',%s)", [PN, e, e])                    # прежняя строка по метке
        pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position, part, qty_per) "
                    "VALUES (%s,'СБОРКА',%s,'ПЛАЗМА',%s,'кп1',2)", [PN, e, e])            # строка с количеством


def test_unblocked_everywhere(pdb, mk):
    _setup(pdb, mk, 12)                                   # 12 − 4 = 8 свободно, нужно 2
    assert dp.compute(PN, 'СБОРКА', 'Б4в', 1)['blocked'] is False
    assert sync._compute_block(PN, 'СБОРКА', None, 'Б4в', own_qty=1) == '✅'
    assert bot._deps_ready(PN, 'Б4в', 'СБОРКА', 1) is True
    assert bot._deps_available_qty(PN, 'Б4в', 'СБОРКА', 1) == (1, 1)
    assert bot._deps_blocking(PN, 'Б4в', 'СБОРКА', 1) == []


def test_blocked_everywhere_when_not_enough(pdb, mk):
    _setup(pdb, mk, 5)                                    # 5 − 4 = 1 свободно, нужно 2
    assert sync._compute_block(PN, 'СБОРКА', None, 'Б4в', own_qty=1) == '⛔ ПЛАЗМА'
    assert bot._deps_ready(PN, 'Б4в', 'СБОРКА', 1) is False
    assert bot._deps_available_qty(PN, 'Б4в', 'СБОРКА', 1) == (0, 1)
    lines = bot._deps_blocking(PN, 'Б4в', 'СБОРКА', 1)
    assert lines and 'кп1' in lines[0]
    rows = [{'project_name': PN, 'sheet_name': 'СБОРКА', 'position': None, 'element': 'Б4в'}]
    assert bot._available_qty_map(rows) == {(PN, 'СБОРКА', 'Б4в'): 0}


def test_available_qty_map_partial(pdb, mk):
    _setup(pdb, mk, 12)
    # у Б4в в плане ещё 6 штук: свободных кп1 хватит на 4 сборки
    mk(4, sheet='СБОРКА', element='Б4в', qty=5, status='ПЛАН')
    rows = [{'project_name': PN, 'sheet_name': 'СБОРКА', 'position': None, 'element': 'Б4в'}]
    assert bot._available_qty_map(rows) == {(PN, 'СБОРКА', 'Б4в'): 4}


def test_element_without_parts_keeps_old_logic(pdb, mk):
    mk(1, sheet='ПЛАЗМА', element='X', qty=10, status='ПЛАН', position='x1')
    mk(2, sheet='СБОРКА', element='X', qty=1, status='ПЛАН')
    pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position) "
                "VALUES (%s,'СБОРКА','X','ПЛАЗМА','X')", [PN])
    assert dp.compute(PN, 'СБОРКА', 'X', 1) is None
    assert sync._compute_block(PN, 'СБОРКА', None, 'X', own_qty=1) == '⛔ ПЛАЗМА'      # прежняя пропорциональная логика
    assert bot._deps_ready(PN, 'X', 'СБОРКА', 1) is False


def test_same_block_ignores_line_order():
    assert sync._same_block('⛔ ПИЛА\n⛔ ПЛАЗМА', '⛔ ПЛАЗМА\n⛔ ПИЛА')
    assert not sync._same_block('⛔ ПИЛА', '✅')


def test_reconcile_uses_parts(pdb, mk, monkeypatch):
    monkeypatch.setattr(sync.sheets, 'update_task_status', lambda *a, **k: None)
    _setup(pdb, mk, 12)
    tid = mk(9, sheet='СБОРКА', element='Б4в', qty=1, status='БЛОК')
    sync._reconcile_blocks(PN)
    assert pdb.fetchone("SELECT status FROM work_orders WHERE id=%s", [tid])['status'] == 'ПЛАН'     # хватает деталей
    pdb.execute("UPDATE work_orders SET status='БЛОК' WHERE id=%s", [tid])
    pdb.execute("UPDATE work_orders SET status='ПЛАН' WHERE project_name=%s AND sheet_name='ПЛАЗМА' AND quantity=12", [PN])
    dp._cache.clear()
    sync._reconcile_blocks(PN)
    assert pdb.fetchone("SELECT status FROM work_orders WHERE id=%s", [tid])['status'] == 'БЛОК'    # деталей не хватает


def test_check_components_reports_problems(pdb, mk):
    import collections
    from src import consistency_check as cc
    # кп1: в листе 6 (готово 2), сборкам нужно 3 × 2 = 6 → норма; пк15: в листе 1, нужно 2 → нехватка
    mk(1, sheet='ПЛАЗМА', element='готовый элемент', qty=2, status='ВЫПОЛНЕНО', position='кп1')
    mk(2, sheet='ПЛАЗМА', element='А', qty=4, status='ПЛАН', position='кп1')
    mk(3, sheet='ПЛАЗМА', element='А', qty=1, status='ПЛАН', position='пк15')
    mk(1, sheet='СБОРКА', element='А', qty=1, status='ВЫПОЛНЕНО')
    mk(2, sheet='СБОРКА', element='Б', qty=2, status='ПЛАН')
    mk(3, sheet='СБОРКА', element='В', qty=1, status='ПЛАН')                        # у В нет количества деталей
    for e, part, q in (('А', 'кп1', 2), ('Б', 'кп1', 2), ('А', 'пк15', 2), ('Б', 'нет_такой', 1), ('Б', 'нет_такой', 1)):
        pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position, part, qty_per) "
                    "VALUES (%s,'СБОРКА',%s,'ПЛАЗМА',%s,%s,%s)", [PN, e, e, part, q])
    warning = collections.defaultdict(list)
    cc.check_components(warning, {PN})
    assert any('нет_такой' in t for _, t in warning['деталь из зависимостей не найдена в листе'])
    assert any('В' in t for _, t in warning['у сборки нет количества деталей'])
    assert any('нет_такой' in t for _, t in warning['дубли строк с деталью в зависимостях'])
    assert any('пк15' in t for _, t in warning['деталей в листе меньше, чем нужно сборкам'])
    assert not any('кп1' in t for _, t in warning['деталей в листе меньше, чем нужно сборкам'])
    # собрана А (1 шт, нужно 2 кп1 + 2 пк15), изготовлено кп1 2, пк15 0 → пк15 «собрано больше, чем изготовлено»
    assert any('пк15' in t for _, t in warning['собрано больше, чем изготовлено деталей'])
    assert not any('кп1' in t for _, t in warning['собрано больше, чем изготовлено деталей'])
