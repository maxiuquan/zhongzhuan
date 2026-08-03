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

#: Ordered migration registry.  Keep ascending by ``version``.
MIGRATIONS: tuple[Migration, ...] = (M001, M003, M004)

__all__ = ["MIGRATIONS", "Migration"]