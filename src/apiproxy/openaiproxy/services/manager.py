# /*********************************************
#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                   O\  =  /O
#                ____/`---'\____
#              .'  \\|     |//  `.
#             /  \\|||  :  |||//  \
#            /  _||||| -:- |||||-  \
#            |   | \\\  -  /// |   |
#            | \_|  ''\---/''  |   |
#            \  .-\__  `-`  ___/-. /
#          ___`. .'  /--.--\  `. . __
#       ."" '<  `.___\_<|>_/___.'  >'"".
#      | | :  `- \`.;`\ _ /`;.`/ - ` : | |
#      \  \ `-.   \_ __\ /__ _/   .-` /  /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='

# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            佛祖保佑       永无BUG
#            心外无法       法外无心
#            三宝弟子       三德子宏愿
# *********************************************/

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING

from openaiproxy.logging import logger

from openaiproxy.utils.concurrency import KeyedMemoryLockManager

if TYPE_CHECKING:
    from openaiproxy.services.base import Service
    from openaiproxy.services.factory import ServiceFactory
    from openaiproxy.services.schema import ServiceType


class NoFactoryRegisteredError(Exception):
    pass


class ServiceManager:
    """Manages the creation of different services."""

    def __init__(self) -> None:
        self.services: dict[str, Service] = {}
        self.factories: dict[str, ServiceFactory] = {}
        self.register_factories()
        self.keyed_lock = KeyedMemoryLockManager()

    def register_factories(self) -> None:
        for factory in self.get_factories():
            try:
                self.register_factory(factory)
            except Exception:  # noqa: BLE001
                logger.exception(f"初始化工厂 {factory} 时发生错误")

    def register_factory(
        self,
        service_factory: ServiceFactory,
    ) -> None:
        """Registers a new factory with dependencies."""
        service_name = service_factory.service_class.name
        self.factories[service_name] = service_factory

    def get(self, service_name: ServiceType, default: ServiceFactory | None = None) -> Service:
        """Get (or create) a service by its name."""
        with self.keyed_lock.lock(service_name):
            if service_name not in self.services:
                self._create_service(service_name, default)

        return self.services[service_name]

    def _create_service(self, service_name: ServiceType, default: ServiceFactory | None = None) -> None:
        """Create a new service given its name, handling dependencies."""
        logger.debug(f"创建{service_name}服务实例...")
        self._validate_service_creation(service_name, default)

        # Create dependencies first
        factory = self.factories.get(service_name)
        if factory is None and default is not None:
            self.register_factory(default)
            factory = default
        if factory is None:
            msg = f"No factory registered for {service_name}"
            raise NoFactoryRegisteredError(msg)
        for dependency in factory.dependencies:
            if dependency not in self.services:
                self._create_service(dependency)

        # Collect the dependent services
        dependent_services = {dep.value: self.services[dep] for dep in factory.dependencies}

        # Create the actual service
        self.services[service_name] = self.factories[service_name].create(**dependent_services)
        self.services[service_name].set_ready()

    def _validate_service_creation(self, service_name: ServiceType, default: ServiceFactory | None = None) -> None:
        """Validate whether the service can be created."""
        if service_name not in self.factories and default is None:
            msg = f"No factory registered for the service class '{service_name.name}'"
            raise NoFactoryRegisteredError(msg)

    def update(self, service_name: ServiceType) -> None:
        """Update a service by its name."""
        if service_name in self.services:
            logger.debug(f"重新加载服务 {service_name}")
            self.services.pop(service_name, None)
            self.get(service_name)

    async def teardown(self) -> None:
        """Teardown all the services."""
        for service in self.services.values():
            if service is None:
                continue
            logger.debug(f"停止{service.name}服务...")
            try:
                await service.teardown()
            except Exception as exc:  # noqa: BLE001
                logger.error(str(exc))
        self.services = {}
        self.factories = {}

    @staticmethod
    def get_factories():
        from openaiproxy.services.factory import ServiceFactory
        from openaiproxy.services.schema import ServiceType

        service_names = [ServiceType(service_type).value.replace("_service", "") for service_type in ServiceType]
        base_module = "openaiproxy.services"
        factories = []

        for name in service_names:
            try:
                module_name = f"{base_module}.{name}.factory"
                module = importlib.import_module(module_name)

                # Find all classes in the module that are subclasses of ServiceFactory
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, ServiceFactory) and obj is not ServiceFactory:
                        factories.append(obj())
                        break

            except Exception as exc:
                logger.exception(exc)
                msg = f"无法初始化服务，请检查你的配置，出错服务：{name}"
                raise RuntimeError(msg) from exc

        return factories


service_manager = ServiceManager()


def initialize_settings_service() -> None:
    """Initialize the settings manager."""
    from openaiproxy.services.settings import factory as settings_factory

    service_manager.register_factory(settings_factory.SettingsServiceFactory())
