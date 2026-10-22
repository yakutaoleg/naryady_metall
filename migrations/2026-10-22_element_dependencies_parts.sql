-- Количество детали на одну сборку (лист ЗАВИСИМОСТИ, колонки F «ДЕТАЛЬ» и G «КОЛ-ВО НА 1 СБОРКУ»).
-- Строки без part/qty_per работают по прежней пропорциональной логике.
ALTER TABLE element_dependencies ADD COLUMN IF NOT EXISTS part varchar(100);
ALTER TABLE element_dependencies ADD COLUMN IF NOT EXISTS qty_per numeric;
CREATE INDEX IF NOT EXISTS element_dependencies_parts_idx ON element_dependencies (project_name, waiting_sheet, element) WHERE part IS NOT NULL;
