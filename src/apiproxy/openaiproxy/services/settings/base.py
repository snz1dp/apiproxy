import contextlib
import json
import os
from pathlib import Path
from shutil import copy2
from typing import Any, Literal

from openaiproxy.utils.version import get_version_info
import yaml
from openaiproxy.logging import logger
from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict
from typing_extensions import override

def is_list_of_any(field: FieldInfo) -> bool:
    """Check if the given field is a list or an optional list of any type.

    Args:
        field (FieldInfo): The field to be checked.

    Returns:
        bool: True if the field is a list or a list of any type, False otherwise.
    """
    if field.annotation is None:
        return False
    try:
        union_args = field.annotation.__args__ if hasattr(field.annotation, "__args__") else []

        return field.annotation.__origin__ is list or any(
            arg.__origin__ is list for arg in union_args if hasattr(arg, "__origin__")
        )
    except AttributeError:
        return False


class MyCustomSource(EnvSettingsSource):
    @override
    def prepare_field_value(self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool) -> Any:  # type: ignore[misc]
        # allow comma-separated list parsing

        # fieldInfo contains the annotation of the field
        if is_list_of_any(field):
            if isinstance(value, str):
                value = value.split(",")
            if isinstance(value, list):
                return value

        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    """Apiproxy Settings Model"""

    code: str = "apiproxy"
    # Define the default APIPROXY_DIR
    config_dir: str | None = None

    dev: bool = False
    """If True, Apiproxy will run in development mode."""
    database_url: str | None = None
    """Database URL for Apiproxy. If not provided, Apiproxy will use a SQLite database."""
    pool_size: int = 10
    """The number of connections to keep open in the connection pool. If not provided, the default is 10."""
    max_overflow: int = 20
    """The number of connections to allow that can be opened beyond the pool size.
    If not provided, the default is 20."""
    db_connect_timeout: int = 20
    """The number of seconds to wait before giving up on a lock to released or establishing a connection to the
    database."""
    database_echo: bool = False

    # sqlite configuration
    sqlite_pragmas: dict | None = {"synchronous": "NORMAL", "journal_mode": "WAL"}
    """SQLite pragmas to use when connecting to the database."""

    # Config
    host: str = "127.0.0.1"
    """The host on which Apiproxy will run."""
    port: int = 8008
    """The port on which Apiproxy will run."""
    workers: int = 1
    """The number of workers to run."""
    log_level: str = "critical"
    """The log level for Apiproxy."""
    log_file: str | None = "logs/apiproxy.log"
    """The path to log file for Apiproxy."""
    alembic_log_file: str = "alembic/alembic.log"
    """The path to log file for Alembic for SQLAlchemy."""


    @field_validator("dev")
    @classmethod
    def set_dev(cls, value):
        from openaiproxy.settings import set_dev

        set_dev(value)
        return value

    @field_validator("log_file", mode="before")
    @classmethod
    def set_log_file(cls, value):
        if isinstance(value, Path):
            value = str(value)
        return value

    @field_validator("config_dir", mode="before")
    @classmethod
    def set_config_dir(cls, value):
        if not value:
            from platformdirs import user_cache_dir

            # Define the app name and author
            app_name = "apiproxy"
            app_author = "snz1dp"

            # Get the cache directory for the application
            cache_dir = user_cache_dir(app_name, app_author)

            # Create a .apiproxy directory inside the cache directory
            value = Path(cache_dir)
            value.mkdir(parents=True, exist_ok=True)

        if isinstance(value, str):
            value = Path(value)
        if not value.exists():
            value.mkdir(parents=True, exist_ok=True)

        return str(value)

    @field_validator("database_url", mode="before")
    @classmethod
    def set_database_url(cls, value, info):
        if not value:
            logger.debug("未设置“database_url”，尝试使用名为“APIPROXY_DATABASE_URL”的环境变量")
            if apiproxy_database_url := os.getenv("APIPROXY_DATABASE_URL"):
                value = apiproxy_database_url
                logger.debug("使用名为“APIPROXY_DATABASE_URL”的环境变量值")
            else:
                logger.debug("未设置“database_url”环境变量，使用SQLite数据库")
                # Originally, we used sqlite:///./apiproxy.db
                # so we need to migrate to the new format
                # if there is a database in that location
                if not info.data["config_dir"]:
                    msg = "配置目录(config_dir)未设置，请设置或提供“database_url”环境变量"
                    raise ValueError(msg)

                from openaiproxy.utils.version import (
                    get_version_info,
                    is_pre_release as apiproxy_is_pre_release
                )

                version = get_version_info()["version"]
                is_pre_release = apiproxy_is_pre_release(version)

                if info.data["save_db_in_config_dir"]:
                    database_dir = info.data["config_dir"]
                    logger.debug(f"保存数据库至配置目录: {database_dir}")
                else:
                    database_dir = Path(__file__).parent.parent.parent.resolve()
                    logger.debug(f"保数据库至当前服务目录: {database_dir}")

                pre_db_file_name = "apiproxy-pre.db"
                db_file_name = "apiproxy.db"
                new_pre_path = f"{database_dir}/{pre_db_file_name}"
                new_path = f"{database_dir}/{db_file_name}"
                final_path = None
                if is_pre_release:
                    if Path(new_pre_path).exists():
                        final_path = new_pre_path
                    elif Path(new_path).exists() and info.data["save_db_in_config_dir"]:
                        # We need to copy the current db to the new location
                        copy2(new_path, new_pre_path)
                    elif Path(f"./{db_file_name}").exists() and info.data["save_db_in_config_dir"]:
                        copy2(f"./{db_file_name}", new_pre_path)
                    else:
                        final_path = new_pre_path
                elif Path(new_path).exists():
                    final_path = new_path
                elif Path(f"./{db_file_name}").exists():
                    try:
                        copy2(f"./{db_file_name}", new_path)
                    except Exception:  # noqa: BLE001
                        logger.exception("复制数据库时出错了，使用缺省数据目录")
                        new_path = f"./{db_file_name}"
                else:
                    final_path = new_path

                if final_path is None:
                    final_path = new_pre_path if is_pre_release else new_path

                value = f"sqlite:///{final_path}"

        return value

    model_config = SettingsConfigDict(validate_assignment=True, extra="ignore", env_prefix="APIPROXY_")

    def update_from_yaml(self, file_path: str, *, dev: bool = False) -> None:
        new_settings = load_settings_from_yaml(file_path)
        self.components_path = new_settings.components_path or []
        self.dev = dev

    def update_settings(self, **kwargs) -> None:
        for key, value in kwargs.items():
            # value may contain sensitive information, so we don't want to log it
            if not hasattr(self, key):
                continue
            if isinstance(getattr(self, key), list):
                # value might be a '[something]' string
                _value = value
                with contextlib.suppress(json.decoder.JSONDecodeError):
                    _value = json.loads(str(value))
                if isinstance(_value, list):
                    for item in _value:
                        _item = str(item) if isinstance(item, Path) else item
                        if _item not in getattr(self, key):
                            getattr(self, key).append(_item)
                else:
                    _value = str(_value) if isinstance(_value, Path) else _value
                    if _value not in getattr(self, key):
                        getattr(self, key).append(_value)

            else:
                setattr(self, key, value)
            logger.debug(f"{key}: {getattr(self, key)}")

    @classmethod
    @override
    def settings_customise_sources(  # type: ignore[misc]
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (MyCustomSource(settings_cls),)

def save_settings_to_yaml(settings: Settings, file_path: str) -> None:
    with Path(file_path).open("w", encoding="utf-8") as f:
        settings_dict = settings.model_dump()
        yaml.dump(settings_dict, f)

def load_settings_from_yaml(file_path: str) -> Settings:
    # Check if a string is a valid path or a file name
    if "/" not in file_path:
        # Get current path
        current_path = Path(__file__).resolve().parent
        _file_path = Path(current_path) / file_path
    else:
        _file_path = Path(file_path)

    with _file_path.open(encoding="utf-8") as f:
        settings_dict = yaml.safe_load(f)
        settings_dict = {k.upper(): v for k, v in settings_dict.items()}

        for key in settings_dict:
            if key not in Settings.model_fields:
                msg = f"配置中未发现“{key}”键值"
                raise KeyError(msg)

    return Settings(**settings_dict)
