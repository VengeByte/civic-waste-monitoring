import platform
import sys

# Work around Windows Python 3.14 alpha platform._wmi_query crash in Shapely/pytest
if sys.platform == "win32" and sys.version_info >= (3, 14):
    platform.uname = lambda: platform._uname_result(("Windows", "localhost", "10", "10.0.22631", "AMD64"))
    platform.system = lambda: "Windows"
    platform.win32_ver = lambda *args, **kwargs: ("10", "10.0.22631", "SP0", "Multiprocessor Free")

import pytest

from backend.circuit_breaker import CIRCUIT_BREAKER
from backend.provider_check import clear_health_cache
from backend.vlm_metrics import VLM_METRICS


@pytest.fixture(autouse=True)
def _reset_vlm_globals():
    """Prevent circuit-breaker and metric state leaking between tests."""
    CIRCUIT_BREAKER.reset()
    VLM_METRICS.reset()
    clear_health_cache()
    yield
    CIRCUIT_BREAKER.reset()
    VLM_METRICS.reset()
    clear_health_cache()
