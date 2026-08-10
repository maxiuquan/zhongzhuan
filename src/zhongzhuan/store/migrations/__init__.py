"""Migration registry for the zhongzhuan store.

Add a new migration by creating ``vNNN_<name>.py`` and appending its
``MIGRATION`` object to :data:`MIGRATIONS` below.  Version numbers must be
monotonic and unique; the engine applies them in ascending order inside
independent transactions.
"""

from __future__ import annotations

from ..migration_engine import Migration
from .v001_baseline import MIGRATION as M001
from .v003_token_hash import MIGRATION as M003
from .v004_response_store import MIGRATION as M004
from .v005_model_capabilities import MIGRATION as M005
from .v006_tool_executions import MIGRATION as M006
from .v007_schema_realign import MIGRATION as M007
from .v008_route_bindings import MIGRATION as M008
from .v009_client_fingerprint import MIGRATION as M009
from .v010_token_cipher import MIGRATION as M010
from .v011_exposure_flag import MIGRATION as M011
from .v012_reasoning_effort_flag import MIGRATION as M012

#: Ordered migration registry.  Keep ascending by ``version``.
MIGRATIONS: tuple[Migration, ...] = (M001, M003, M004, M005, M006, M007, M008, M009, M010, M011, M012)

__all__ = ["MIGRATIONS", "Migration"]
