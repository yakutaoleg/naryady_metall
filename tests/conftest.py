"""Общие настройки тестов. Тесты с БД работают на изолированном проекте PN и чистят за собой."""
import sys
import pytest

sys.path.insert(0, '/root/naryady/prod')

PN = '__pytest__'


def _clean(db):
    db.execute("DELETE FROM work_order_history WHERE work_order_id IN "
               "(SELECT id FROM work_orders WHERE project_name=%s)", [PN])
    db.execute("DELETE FROM element_dependencies WHERE project_name=%s", [PN])
    db.execute("DELETE FROM work_orders WHERE project_name=%s", [PN])


@pytest.fixture
def pdb():
    from src import db
    _clean(db)
    yield db
    _clean(db)


@pytest.fixture
def mk(pdb):
    """Создаёт строку work_orders в тестовом проекте, возвращает её id."""
    import uuid

    def _mk(row_num, sheet='СВАРКА', element='E', qty=1, status='ПЛАН', row_id=None, **extra):
        cols = dict(project_name=PN, file_id='F', sheet_name=sheet, row_num=row_num,
                    row_id=row_id or str(uuid.uuid4()), element=element, quantity=qty, status=status)
        cols.update(extra)
        r = pdb.fetchone(
            f"INSERT INTO work_orders ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) RETURNING id",
            list(cols.values()))
        return r['id']
    return _mk
