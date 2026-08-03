"""Responses Bridge v3 resource layer (T21 / R-P1-28..33 / R-P0-40).

The ``responses_v3`` package implements the six official ``/v1/responses``
resource endpoints on top of :class:`~zhongzhuan.store.response_store.ResponseStore`:

    POST   /v1/responses                      -> create
    GET    /v1/responses/{id}                 -> retrieve
    DELETE /v1/responses/{id}                 -> delete
    POST   /v1/responses/{id}/cancel          -> cancel
    POST   /v1/responses/compact              -> compact (honest 501 stub)
    GET    /v1/responses/{id}/input_items     -> input_items (paginated)

T22 adds :mod:`.chain`: ``previous_response_id`` recovery with the R-P0-29
cycle / depth / budget guards, feeding a reasoning-free replay array.

It is the successor to the legacy ``/v1/responses`` handler and is selected by
the v3 feature switch (T12).  This module is a **skeleton** on the critical
path: persistence + object mapping + routing are real and tested; the live
upstream streaming pipeline is wired in T24/T28 and the SDK contract is sealed
in T37.
"""
from __future__ import annotations

from .chain import ChainResolution, ChainResolver, build_upstream_input
from .handler import ResponsesV3Handler
from .schema import to_error_object, to_input_items_list, to_response_object

__all__ = [
    "ResponsesV3Handler",
    "ChainResolution",
    "ChainResolver",
    "build_upstream_input",
    "to_response_object",
    "to_input_items_list",
    "to_error_object",
]
