from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import command, util
from alembic.config import Config
from openaiproxy.logging import logger
from sqlalchemy import event, inspect
from sqlalchemy.dialects import sqlite as dialect_sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import Session, SQLModel, create_engine, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from openaiproxy.services.base import Service
from openaiproxy.services.database import models
from openaiproxy.services.database.utils import Result, TableResults
from openaiproxy.services.deps import get_settings_service
from openaiproxy.services.utils import teardown_superuser
from openaiproxy.utils.timezone import current_timezone

if TYPE_CHECKING:
    from openaiproxy.services.settings.service import SettingsService

class DatabaseService(Service):
    name = "database_service"

    def __init__(self, settings_service: SettingsService):
        self.settings_service = settings_service
        if settings_service.settings.database_url is None:
            msg = "No database URL provided"
            raise ValueError(msg)
        self.database_url: str = settings_service.settings.database_url
        self._sanitize_database_url()
        # This file is in openaiproxy.services.database.manager.py
        # the ini is in openaiproxy
        openaiproxy_dir = Path(__file__).parent.parent.parent
        self.script_location = openaiproxy_dir / "alembic"
        self.alembic_cfg_path = openaiproxy_dir / "alembic.ini"
        # register the event listener for sqlite as part of this class.
        # Using decorator will make the method not able to use self
        event.listen(Engine, "connect", self.on_connection)
        self.engine = self._create_engine()
        self.async_engine = self._create_async_engine()
        alembic_log_file = self.settings_service.settings.alembic_log_file

        # Check if the provided path is absolute, cross-platform.
        if Path(alembic_log_file).is_absolute():
            # Use the absolute path directly.
            self.alembic_log_path = Path(alembic_log_file)
        else:
            # Construct the path using the openaiproxy directory.
            self.alembic_log_path = Path(openaiproxy_dir) / alembic_log_file

    def reload_engine(self) -> None:
        self._sanitize_database_url()
        self.engine = self._create_engine()
        self.async_engine = self._create_async_engine()

    def _sanitize_database_url(self):
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://")
            logger.warning(
                "Fixed postgres dialect in database URL. Replacing postgres:// with postgresql://. "
                "To avoid this warning, update the database URL."
            )

    def _create_engine(self) -> Engine:
        """Create the engine for the database."""
        return create_engine(
            self.database_url,
            connect_args=self._get_connect_args(),
            pool_size=self.settings_service.settings.pool_size,
            max_overflow=self.settings_service.settings.max_overflow,
            echo=self.settings_service.settings.database_echo
        )

    def _create_async_engine(self) -> AsyncEngine:
        """Create the engine for the database."""
        url_components = self.database_url.split("://", maxsplit=1)
        if url_components[0].startswith("sqlite"):
            database_url = "sqlite+aiosqlite://"
            kwargs = {}
        else:
            kwargs = {
                "pool_size": self.settings_service.settings.pool_size,
                "max_overflow": self.settings_service.settings.max_overflow,
                "echo": self.settings_service.settings.database_echo,
            }
            database_url = "postgresql+psycopg://" if url_components[0].startswith("postgresql") else url_components[0]
        database_url += url_components[1]
        return create_async_engine(
            database_url,
            connect_args=self._get_connect_args(),
            **kwargs,
        )

    def _get_connect_args(self):
        if self.settings_service.settings.database_url and self.settings_service.settings.database_url.startswith(
            "sqlite"
        ):
            connect_args = {
                "check_same_thread": False,
                "timeout": self.settings_service.settings.db_connect_timeout,
            }
        else:
            connect_args = {}
        return connect_args

    def on_connection(self, dbapi_connection, _connection_record) -> None:
        if isinstance(dbapi_connection, sqlite3.Connection | dialect_sqlite.aiosqlite.AsyncAdapt_aiosqlite_connection):
            pragmas: dict = self.settings_service.settings.sqlite_pragmas or {}
            pragmas_list = []
            for key, val in pragmas.items():
                pragmas_list.append(f"PRAGMA {key} = {val}")
            logger.info(f"sqlite connection, setting pragmas: {pragmas_list}")
            if pragmas_list:
                cursor = dbapi_connection.cursor()
                try:
                    for pragma in pragmas_list:
                        try:
                            cursor.execute(pragma)
                        except OperationalError:
                            logger.exception(f"Failed to set PRAGMA {pragma}")
                finally:
                    cursor.close()

    @contextmanager
    def with_session(self):
        with Session(self.engine) as session:
            yield session

    @asynccontextmanager
    async def with_async_session(self):
        async with AsyncSession(self.async_engine, expire_on_commit=False) as session:
            yield session

    def check_schema_health(self) -> bool:
        inspector = inspect(self.engine)

        model_mapping: dict[str, type[SQLModel]] = {
            "openaiapi_nodes": models.Node,
            "openaiapi_node_statuses": models.NodeStatus,
        }

        # To account for tables that existed in older versions
        legacy_tables = ["flowstyle"]

        for table, model in model_mapping.items():
            expected_columns = list(model.model_fields.keys())

            try:
                available_columns = [col["name"] for col in inspector.get_columns(table)]
            except sa.exc.NoSuchTableError:
                logger.debug(f"Missing table: {table}")
                return False

            for column in expected_columns:
                if column not in available_columns:
                    logger.debug(f"Missing column: {column} in table {table}")
                    return False

        for table in legacy_tables:
            if table in inspector.get_table_names():
                logger.warning(f"Legacy table exists: {table}")

        return True

    def init_alembic_cfg(self) -> tuple[Config, bool, str]:
        """Initialize alembic if needed."""
        buffer = self.alembic_log_path.open("w", encoding="utf-8")
        alembic_cfg = Config(stdout=buffer)

        # alembic_cfg.attributes["connection"] = session
        alembic_cfg.set_main_option("script_location", str(self.script_location))
        alembic_cfg.set_main_option("sqlalchemy.url", self.database_url.replace("%", "%%"))

        should_initialize_alembic = False
        database_alembic_version = None
        with self.with_session() as session:
            # If the table does not exist it throws an error
            # so we need to catch it
            try:
                database_alembic_version = session.exec(text(f"SELECT * FROM apiproxy_alembic_version")).first()
            except Exception:  # noqa: BLE001
                logger.debug("数据结构未初始化")
                should_initialize_alembic = True
        return alembic_cfg, should_initialize_alembic, database_alembic_version

    def run_migrations(self, *, fix=False) -> None:
        # First we need to check if alembic has been initialized
        # If not, we need to initialize it
        # if not self.script_location.exists(): # this is not the correct way to check if alembic has been initialized
        # We need to check if the version_table table exists
        # if not, we need to initialize alembic
        # stdout should be something like sys.stdout
        # which is a buffer
        # I don't want to output anything
        # subprocess.DEVNULL is an int
        alembic_cfg, should_initialize_alembic, _ = self.init_alembic_cfg()
        try:
            if should_initialize_alembic:
                try:
                    command.ensure_version(alembic_cfg)
                    # alembic_cfg.attributes["connection"].commit()
                    command.upgrade(alembic_cfg, "head")
                except Exception as exc:
                    alembic_cfg.stdout.close()
                    msg = "初始化数据结构时出错"
                    logger.exception(msg)
                    raise RuntimeError(msg) from exc

            logger.info(f"在{self.script_location}目录运行数据结构检测操作...")
            try:
                alembic_cfg.stdout.write(f"{datetime.now(tz=current_timezone()).astimezone()}: 检查数据结构升级...\n")
                command.check(alembic_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"数据结构升级检查时发现问题: {exc}")
                if isinstance(exc, util.exc.CommandError | util.exc.AutogenerateDiffsDetected):
                    command.upgrade(alembic_cfg, "head")
                    time.sleep(3)

            try:
                alembic_cfg.stdout.write(f"{datetime.now(tz=current_timezone()).astimezone()}: 检查数据结构升级...\n")
                command.check(alembic_cfg)
            except util.exc.AutogenerateDiffsDetected as exc:
                if not fix:
                    logger.warning(f"数据模型与数据库结构不一致：\n{exc}")
                    return

            if fix:
                self.try_downgrade_upgrade_until_success(alembic_cfg)
        finally:
            # Close the buffer
            alembic_cfg.stdout.close()

    def try_downgrade_upgrade_until_success(self, alembic_cfg, retries=5) -> None:
        # Try -1 then head, if it fails, try -2 then head, etc.
        # until we reach the number of retries
        for i in range(1, retries + 1):
            try:
                command.check(alembic_cfg)
                break
            except util.exc.AutogenerateDiffsDetected as exc:
                # downgrade to base and upgrade again
                logger.warning("数据结构差异比较检测到不一致")
                command.downgrade(alembic_cfg, f"-{i}")
                # wait for the database to be ready
                time.sleep(3)
                command.upgrade(alembic_cfg, "head")

    def run_migrations_test(self):
        # This method is used for testing purposes only
        # We will check that all models are in the database
        # and that the database is up to date with all columns
        # get all models that are subclasses of SQLModel
        sql_models = [
            model for model in models.__dict__.values() if isinstance(model, type) and issubclass(model, SQLModel)
        ]
        return [TableResults(sql_model.__tablename__, self.check_table(sql_model)) for sql_model in sql_models]

    def check_table(self, model):
        results = []
        inspector = inspect(self.engine)
        table_name = model.__tablename__
        expected_columns = list(model.__fields__.keys())
        available_columns = []
        try:
            available_columns = [col["name"] for col in inspector.get_columns(table_name)]
            results.append(Result(name=table_name, type="table", success=True))
        except sa.exc.NoSuchTableError:
            logger.exception(f"Missing table: {table_name}")
            results.append(Result(name=table_name, type="table", success=False))

        for column in expected_columns:
            if column not in available_columns:
                logger.error(f"Missing column: {column} in table {table_name}")
                results.append(Result(name=column, type="column", success=False))
            else:
                results.append(Result(name=column, type="column", success=True))
        return results

    def create_db_and_tables(self) -> None:
        from sqlalchemy import inspect

        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()
        current_tables = [
            "openaiapi_nodes",
            "openaiapi_node_statuses",
        ]

        if table_names and all(table in table_names for table in current_tables):
            logger.debug("数据库表结构已存在")
            return

        alembic_cfg, should_initialize_alembic, database_alembic_version = self.init_alembic_cfg()
        if should_initialize_alembic:
            command.ensure_version(alembic_cfg)

        alembic_cfg.stdout.close()

        if database_alembic_version is not None:
            return

        logger.debug("创建数据库表结构...")
        for table in SQLModel.metadata.sorted_tables:
            try:
                table.create(self.engine, checkfirst=True)
            except OperationalError as oe:
                logger.warning(f"数据表{table}已存在，已忽略创建，异常信息: {oe}")
            except Exception as exc:
                msg = f"创建数据表{table}时出错"
                logger.exception(msg)
                raise RuntimeError(msg) from exc

        with self.with_session() as session:
            # TODO: 每次升级结构时一定要把最新的版本放这里
            last_version = "289442e9b00c"
            session.exec(text(f"insert into apiproxy_alembic_version (version_num) values ('{last_version}');"))
            session.commit()

        # Now check if the required tables exist, if not, something went wrong.
        inspector = inspect(self.engine)
        table_names = inspector.get_table_names()
        for table in current_tables:
            if table not in table_names:
                logger.error("创建数据库和数据表时出错")
                logger.error("请检查数据库配置")
                msg = "创建数据库和数据表时出错"
                raise RuntimeError(msg)

        logger.debug("数据库和数据表创建成功")

    async def teardown(self) -> None:
        logger.debug("正在关闭数据库服务...")
        try:
            settings_service = get_settings_service()
            # using the SUPERUSER to get the user
            async with self.with_async_session() as session:
                await teardown_superuser(settings_service, session)
        except Exception:  # noqa: BLE001
            logger.exception("关闭数据库服务时出错")
        await self.async_engine.dispose()
        await asyncio.to_thread(self.engine.dispose)
