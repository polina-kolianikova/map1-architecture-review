from pathlib import Path
from alembic import command
from alembic.config import Config

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    command.upgrade(Config(str(root / "backend/alembic.ini")), "head")
