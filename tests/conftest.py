import pytest

from openmovehub.config import Settings
from openmovehub.services.connection_manager import ConnectionManager


@pytest.fixture
def mock_settings():
    return Settings(host="127.0.0.1", port=8341)


@pytest.fixture
def manager():
    return ConnectionManager()
