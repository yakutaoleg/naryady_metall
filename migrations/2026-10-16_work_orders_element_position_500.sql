-- Длинные списки элементов в одной ячейке (до ~100 штук) не помещались в varchar(100).
ALTER TABLE work_orders ALTER COLUMN element TYPE varchar(500), ALTER COLUMN position TYPE varchar(500);
