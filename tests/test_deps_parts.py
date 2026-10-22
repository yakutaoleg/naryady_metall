"""Зависимости с количеством деталей на сборку: разбор исходного файла и расчёт доступности."""
from src import deps_parts as dp


def _rows():
    return [
        ('Позиция', 'Сечение', 'Кол-во', 'Длина (мм)', 'Класс стали', 'Масса', None),
        (None, None, None, None, None, 'элемента (кг)', 'всего (кг)'),
        ('Поз.=Б3 Количество=2 Масса всего=10(кг) ', None, None, None, None, None, None),
        ('кп1', 'Пластина 25x340', 2, 340, 'С255', 21.65, 43.3),
        ('уп5', 'L 100x7', 4, 240, 'С245', 2.5, 10),
        ('Поз.=Б4в Количество=1 Масса всего=5(кг) ', None, None, None, None, None, None),
        ('кп1', 'Пластина 25x340', 2, 340, 'С255', 21.65, 43.3),
        ('Итого (кг)', None, None, None, None, None, 99),
        ('Поз.=Б3 Количество=1 Масса всего=5(кг) ', None, None, None, None, None, None),   # второй блок той же сборки
        ('кп1', 'Пластина 25x340', 2, 340, 'С255', 21.65, 43.3),
    ]


def test_parse_components_and_per_assembly():
    asm, need = dp.parse_assembly_components(_rows())
    assert asm == {'Б3': 3.0, 'Б4в': 1.0}
    assert need['Б3']['кп1'] == 2 * 2 + 2 * 1          # блок 2 шт × 2 + блок 1 шт × 2
    assert need['Б3']['уп5'] == 8
    assert need['Б4в'] == {'кп1': 2.0}                 # строка «Итого» не деталь
    per = dp.per_assembly(asm, need)
    assert per['Б3']['кп1'] == 2.0 and per['Б4в']['кп1'] == 2.0


def test_parse_new_style_grouper():
    rows = [('Позиция', 'Кол-во'), ('СТОЛБ. Количество=3', None), ('п1', 5)]
    asm, need = dp.parse_assembly_components(rows)
    assert asm == {'СТОЛБ': 3.0} and need['СТОЛБ'] == {'п1': 15.0}


def _ctx(done_kp1, done_asm, extra_parts=()):
    # Б4, Б4б — уже готовы; Б4в и Б3 ещё нет. Каждой нужно по 2 кп1.
    parts = [('Б4', 'ПЛАЗМА', 'кп1', 2.0), ('Б4б', 'ПЛАЗМА', 'кп1', 2.0),
             ('Б4в', 'ПЛАЗМА', 'кп1', 2.0), ('Б3', 'ПЛАЗМА', 'кп1', 2.0)] + list(extra_parts)
    return dp.Ctx(parts, done_asm, {('ПЛАЗМА', 'кп1'): (40.0, float(done_kp1))})


def test_partial_unblock_example_b4v():
    """Вырезано 12 кп1, Б4 и Б4б уже собраны (по 2) → свободно 8 → Б4в (нужно 2) не заблокирован."""
    res = dp.evaluate(_ctx(12, {'Б4': 1, 'Б4б': 1}), 'Б4в', 1)
    assert res['avail'] == 4 and not res['blocked']
    assert dp.format_block(res, 1) == '✅'


def test_blocked_when_not_enough():
    res = dp.evaluate(_ctx(5, {'Б4': 1, 'Б4б': 1}), 'Б4в', 1)       # 5 − 4 = 1 свободно, нужно 2
    assert res['blocked'] and res['avail'] == 0
    assert dp.format_block(res, 1) == '⛔ ПЛАЗМА'
    assert 'кп1' in dp.blocking_lines(res)[0]


def test_partial_text_when_several_pieces():
    res = dp.evaluate(_ctx(12, {'Б4': 1, 'Б4б': 1}), 'Б3', 6)       # свободно 8 → можно 4 сборки из 6
    assert res['avail'] == 4 and res['blocked']
    assert dp.format_block(res, 6).startswith('✅ 4 из 6')


def test_overconsumption_does_not_go_negative():
    res = dp.evaluate(_ctx(2, {'Б4': 1, 'Б4б': 1}), 'Б4в', 1)       # собрано больше, чем вырезано
    assert res['avail'] == 0 and res['blocked']


def test_min_over_parts_and_unknown_part_ignored():
    parts = [('А', 'ПЛАЗМА', 'п1', 2.0), ('А', 'ПИЛА', 'п2', 1.0), ('А', 'ПИЛА', 'нет_такой', 3.0)]
    ctx = dp.Ctx(parts, {}, {('ПЛАЗМА', 'п1'): (10.0, 10.0), ('ПИЛА', 'п2'): (10.0, 3.0)})
    res = dp.evaluate(ctx, 'А', 1)
    assert res['avail'] == 3 and not res['blocked']                  # п1 даёт 5, п2 даёт 3; детали «нет_такой» в листе нет
    assert res['covered'] == {'ПЛАЗМА', 'ПИЛА'}


def test_no_parts_returns_none():
    assert dp.evaluate(_ctx(12, {}), 'Б99', 1) is None
    ctx = dp.Ctx([('А', 'ПЛАЗМА', 'x', 1.0)], {}, {})                # детали нет ни в одном листе
    assert dp.evaluate(ctx, 'А', 1) is None
