import sys; sys.path.insert(0, '/root/naryady/prod')
from src.project_wizard import collect_pos_assemblies

M = {0: 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 1: 'СЕЧЕНИЕ', 2: 'КОЛ-ВО'}
ASM = [('Поз.=Б1 Количество=2 Масса всего=1(кг)', None, None), ('дб3', 'ДБ', 1), ('пб1', 'Пл', 1), (None, None, None),
       ('Поз.=Б1а Количество=2', None, None), ('дб3', 'ДБ', 1), ('Итого (кг)', None, None)]
FLAT = [('дб3', 'ДБ', 4), ('пб1', 'Пл', 8), ('кр1', 'КРУГ', 6)]


def test_order_independent():
    a = collect_pos_assemblies([(ASM, M), (FLAT, M)])
    b = collect_pos_assemblies([(FLAT, M), (ASM, M)])
    assert a == b == {'дб3': ['Б1', 'Б1а'], 'пб1': ['Б1']}


def test_flat_has_nothing():
    assert collect_pos_assemblies([(FLAT, M)]) == {}
