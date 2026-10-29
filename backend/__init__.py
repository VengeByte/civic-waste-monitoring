"""Backend module for Civic Garbage Reporting System."""

import platform
import sys

# Work around Windows Python 3.14 alpha platform._wmi_query crash in Shapely
if sys.platform == "win32" and sys.version_info >= (3, 14):
    platform.uname = lambda: platform._uname_result(("Windows", "localhost", "10", "10.0.22631", "AMD64"))
    platform.system = lambda: "Windows"
    platform.win32_ver = lambda *args, **kwargs: ("10", "10.0.22631", "SP0", "Multiprocessor Free")
