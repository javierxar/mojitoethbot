import os
import tempfile

# DB temporal para los tests: se define antes de importar app.* (config lee el entorno al importar)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test_eth.db")
os.environ["REDIS_ENABLED"] = "false"

import pytest  # noqa: E402


@pytest.fixture
def db():
    from app.database import SessionLocal, init_db
    from app.models import EthBotState, EthTrade, Setting

    init_db()
    session = SessionLocal()
    yield session
    session.rollback()
    for model in (EthTrade, EthBotState, Setting):
        session.query(model).delete()
    session.commit()
    session.close()
