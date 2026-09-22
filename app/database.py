import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.models import Base

logger = logging.getLogger(__name__)

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False},  # necesario para SQLite con FastAPI
    echo=False,
)


# Habilitar WAL mode en SQLite para mejor concurrencia
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Crea todas las tablas si no existen y aplica migraciones ligeras."""
    Base.metadata.create_all(bind=engine)
    _migrate_columns()
    logger.info("Base de datos inicializada en: %s", config.DATABASE_URL)


def _migrate_columns() -> None:
    """Agrega columnas nuevas a tablas existentes sin borrar datos (SQLite ALTER TABLE)."""
    migrations: list[str] = [
        # Reservado para futuras migraciones ligeras del bot ETH.
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # La columna ya existe


def get_db() -> Generator[Session, None, None]:
    """Dependency de FastAPI para inyectar sesión de DB."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """Context manager para usar DB fuera de FastAPI (scheduler, runner)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_setting(db: Session, key: str, default=None):
    """Lee un setting de la DB. Devuelve default si no existe."""
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    """Escribe o actualiza un setting en la DB."""
    from datetime import datetime
    from app.models import Setting

    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(Setting(key=key, value=value))
    db.flush()
