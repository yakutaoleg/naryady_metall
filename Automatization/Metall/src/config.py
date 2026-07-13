import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

TG_TOKEN        = os.environ['TG_TOKEN']
DB_HOST         = os.environ.get('DB_HOST', 'localhost')
DB_PORT         = int(os.environ.get('DB_PORT', 5432))
DB_NAME         = os.environ['DB_NAME']
DB_USER         = os.environ['DB_USER']
DB_PASS         = os.environ['DB_PASS']
GOOGLE_SA_KEY   = os.environ['GOOGLE_SA_KEY_PATH']
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
TEST_MODE       = os.environ.get('TEST_MODE', 'true').lower() == 'true'
LOG_LEVEL       = os.environ.get('LOG_LEVEL', 'INFO')

WORK_SHEETS = ['ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА', 'ПОКРАСКА']
ACTIVE_FILE_NAME   = os.environ.get('ACTIVE_FILE_NAME', 'Наряды')
INACTIVE_FILE_MARK = 'закрыт'
