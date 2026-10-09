"""Логика, которая раньше ломала данные: остатки, устаревшие копии, ручные блокировки. Изолированный проект __pytest__."""
import uuid
from tests.conftest import PN
from src import sync


def test_stale_copy_removed_temp_row_kept(pdb, mk):
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    mk(1, row_id=a); mk(2, row_id=b); mk(3, row_id=a); mk(-77, row_id=c)
    n = sync._delete_stale_positional(PN, 'СВАРКА', [1, 2], [a, b])
    left = sorted(r['row_num'] for r in pdb.fetchall("SELECT row_num FROM work_orders WHERE project_name=%s", [PN]))
    assert n == 1 and left == [-77, 1, 2]
    assert sync._delete_stale_positional(PN, 'СВАРКА', [1, 2], [a, b]) == 0   # повтор ничего не удаляет


def test_partial_is_atomic_and_creates_remainder(pdb, mk):
    from src import bot
    tid = mk(5, qty=10, status='ПЛАН')
    task = pdb.fetchone("SELECT * FROM work_orders WHERE id=%s", [tid])
    new_rid = str(uuid.uuid4())
    bot._apply_partial_db(tid, task, 4, 4, 10, None, 0, new_rid)
    done = pdb.fetchone("SELECT status, qty_done, quantity, date_fact FROM work_orders WHERE id=%s", [tid])
    assert (done['status'], int(done['qty_done']), int(done['quantity'])) == ('ВЫПОЛНЕНО', 4, 4)
    assert done['date_fact'] is not None
    rest = pdb.fetchone("SELECT status, quantity, row_num FROM work_orders WHERE row_id=%s", [new_rid])
    assert (rest['status'], int(rest['quantity']), rest['row_num']) == ('ПЛАН', 6, -tid)
    assert pdb.fetchone("SELECT count(*) c FROM work_order_history WHERE work_order_id=%s", [tid])['c'] == 1


def test_partial_rolls_back_on_error(pdb, mk):
    from src import bot
    tid = mk(6, qty=10)
    task = pdb.fetchone("SELECT * FROM work_orders WHERE id=%s", [tid])
    try:
        bot._apply_partial_db(tid, task, 4, 4, 10, None, 0, 'не-uuid')     # row_id остатка невалиден: ошибка внутри транзакции
    except Exception:
        pass
    after = pdb.fetchone("SELECT status, quantity FROM work_orders WHERE id=%s", [tid])
    assert after['status'] == 'ПЛАН' and int(after['quantity']) == 10
    assert pdb.fetchone("SELECT count(*) c FROM work_order_history WHERE work_order_id=%s", [tid])['c'] == 0


def test_manual_block_not_cleared(pdb, mk):
    pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position) "
                "VALUES (%s,'СВАРКА','E','ПЛАЗМА','X')", [PN])
    tid = mk(7, status='БЛОК', comment='нет материала')
    sync._reconcile_blocks(PN)
    assert pdb.fetchone("SELECT status FROM work_orders WHERE id=%s", [tid])['status'] == 'БЛОК'


def test_auto_block_cleared_when_deps_done(pdb, mk, monkeypatch):
    monkeypatch.setattr(sync.sheets, 'update_task_status', lambda *a, **k: None)
    pdb.execute("INSERT INTO element_dependencies (project_name, waiting_sheet, element, requires_sheet, requires_position) "
                "VALUES (%s,'СВАРКА','E','ПЛАЗМА','X')", [PN])
    mk(8, sheet='ПЛАЗМА', element='X', status='ВЫПОЛНЕНО')
    tid = mk(9, status='БЛОК')
    sync._reconcile_blocks(PN)
    assert pdb.fetchone("SELECT status FROM work_orders WHERE id=%s", [tid])['status'] == 'ПЛАН'


def test_orphan_partial_repair_dry_run_changes_nothing(pdb):
    before = pdb.fetchone("SELECT count(*) c FROM work_orders", [])['c']
    sync._repair_orphan_partials(dry_run=True)
    assert pdb.fetchone("SELECT count(*) c FROM work_orders", [])['c'] == before
