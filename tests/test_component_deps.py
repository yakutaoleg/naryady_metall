"""Строки с деталями для листа ЗАВИСИМОСТИ: сборка из разобранного файла и сверка с листами деталей."""
from src import component_deps as cd


def _data():
    asm = {'Б3': 2.0, 'Б4в': 1.0, 'Ф': 1.0}
    need = {'Б3': {'кп1': 4.0, 'уп5': 8.0}, 'Б4в': {'кп1': 2.0, 'пк15': 2.0}, 'Ф': {'х9': 1.0}}
    elements = {'б3': 'Б3', 'б4в': 'Б4в'}                        # Ф в листе СБОРКА нет
    pos_spec = {'кп1': 'ПЛАЗМА', 'уп5': 'ПИЛА', 'пк15': 'ПЛАЗМА'}   # х9 нигде нет
    pos_total = {('ПЛАЗМА', 'кп1'): 40.0, ('ПИЛА', 'уп5'): 8.0, ('ПЛАЗМА', 'пк15'): 1.0}
    return asm, need, elements, pos_spec, pos_total


def test_build_rows_and_report():
    rows, rep = cd.build_rows(*_data(), standalone={('ПЛАЗМА', 'кп1')})
    assert ['СБОРКА', 'Б3', 'ПЛАЗМА', 'Б3', 'кп1', 2.0] in rows        # 4 шт на 2 сборки = 2 на сборку
    assert ['СБОРКА', 'Б3', 'ПИЛА', 'Б3', 'уп5', 4.0] in rows
    assert ['СБОРКА', 'Б4в', 'ПЛАЗМА', 'Б4в', 'пк15', 2.0] in rows
    assert rep['not_in_sheet'] == ['Ф']                                   # сборки из файла, которых нет в листе
    assert rep['unresolved'] == []                                        # Ф пропущена целиком, до деталей не дошли
    # пк15: в листе 1, нужно 2 → нехватка; кп1: в листе 40, нужно 6, но это «готовый элемент» → избыток не считаем
    assert ('ПЛАЗМА', 'пк15', 1.0, 2.0, 'нехватка') in rep['mismatches']
    assert not any(m[1] == 'кп1' for m in rep['mismatches'])


def test_excess_reported_when_not_standalone():
    rows, rep = cd.build_rows(*_data(), standalone=set())
    assert any(m[1] == 'кп1' and m[4] == 'избыток' for m in rep['mismatches'])


def test_unresolved_detail():
    asm, need, elements, pos_spec, pos_total = _data()
    elements['ф'] = 'Ф'
    rows, rep = cd.build_rows(asm, need, elements, pos_spec, pos_total)
    assert ('Ф', 'х9') in rep['unresolved'] and rep['not_in_sheet'] == []


def test_assembly_files_from_source_map():
    sm = [{'spec': 'СБОРКА', 'entries': [{'file': {'id': 'a'}, 'active': True}, {'file': {'id': 'b'}, 'active': False},
                                         {'file': {'id': 'a'}, 'active': True}]},
          {'spec': 'ПЛАЗМА', 'entries': [{'file': {'id': 'c'}}]}]
    assert cd.assembly_files_from_source_map(sm) == [{'id': 'a'}]
