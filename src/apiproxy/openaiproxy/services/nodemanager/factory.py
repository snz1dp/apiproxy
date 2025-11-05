import os
from openaiproxy.services.factory import ServiceFactory
from openaiproxy.services.nodemanager.service import NodeManager

class NodeManagerFactory(ServiceFactory):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        super().__init__(NodeManager)

    def create(self):
        # Here you would have logic to create and configure a SettingsService

        return NodeManager(config_path=os.getenv('CONFIG_FILE'))
