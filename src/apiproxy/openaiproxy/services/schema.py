from enum import Enum


class ServiceType(str, Enum):
    """Enum for the different types of services that can be registered with the service manager."""

    SETTINGS_SERVICE = "settings_service"
    DATABASE_SERVICE = "database_service"
    NODEMANAGER_SERVICE = "nodemanager_service"