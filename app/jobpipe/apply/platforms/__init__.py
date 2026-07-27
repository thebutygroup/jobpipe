"""Platform appliers: per-ATS application behaviour behind one contract.

Registry pattern mirrors jobpipe.sources — adding platform N+1 is one class
+ one register() call, no core changes.
"""

from . import greenhouse, js_boards  # noqa: F401  (imports wire the registry)
from .base import PlatformApplier, get_applier, register  # noqa: F401
