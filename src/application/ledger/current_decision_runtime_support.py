from __future__ import annotations

from .current_decision_common import (
    Any,
    CURRENT_DECISION_PROJECTION_SCHEMA,
    CurrentDecisionProjectionError,
    Mapping,
    POSITION_PROJECTION_SCHEMA,
    _GENERATION_FIELDS,
    _integer,
)

from .current_decision_payload import (
    _decode_projection_row_payload,
)

def _decision_generations(row: Mapping[str, Any] | None) -> tuple[int, ...]:
    if row is None:
        return tuple(0 for _field in _GENERATION_FIELDS)
    item = dict(row)
    return tuple(_integer(item.get(field), field=field) for field in _GENERATION_FIELDS)

def _projection_bindings_clean(
    *,
    account: str,
    source: Mapping[str, Any] | None,
    head: Mapping[str, Any] | None,
    generation: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
    implementation_fingerprint: str,
) -> bool:
    if any(value is None for value in (source, head, generation, projection)):
        return False
    try:
        source_row = dict(source or {})
        head_row = dict(head or {})
        generation_row = dict(generation or {})
        projection_row = dict(projection or {})
        return all(
            (
                projection_row.get("account") == account,
                projection_row.get("projection_schema")
                == CURRENT_DECISION_PROJECTION_SCHEMA,
                projection_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                source_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
                head_row.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
                source_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                head_row.get("projector_implementation_fingerprint")
                == implementation_fingerprint,
                head_row.get("status") == "trusted",
                head_row.get("built_source_generation")
                == source_row.get("source_generation"),
                head_row.get("built_lots_generation") == head_row.get("lots_generation"),
                projection_row.get("built_position_source_generation")
                == source_row.get("source_generation"),
                projection_row.get("built_position_lots_generation")
                == head_row.get("lots_generation"),
                projection_row.get("position_lots_fingerprint")
                == head_row.get("projection_fingerprint"),
                projection_row.get("built_decision_input_generation")
                == generation_row.get("generation"),
                projection_row.get("built_case_generation")
                == generation_row.get("case_generation"),
                projection_row.get("built_evidence_generation")
                == generation_row.get("evidence_generation"),
                projection_row.get("built_allocation_generation")
                == generation_row.get("allocation_generation"),
                projection_row.get("built_source_consumption_generation")
                == generation_row.get("source_consumption_generation"),
                projection_row.get("built_timing_generation")
                == generation_row.get("timing_generation"),
                projection_row.get("built_combo_identity_generation")
                == generation_row.get("combo_identity_generation"),
                projection_row.get("built_assigned_stock_generation")
                == generation_row.get("assigned_stock_generation"),
            )
        )
    except (CurrentDecisionProjectionError, TypeError, ValueError):
        return False

def _projection_metadata_clean(
    *,
    account: str,
    source: Mapping[str, Any] | None,
    head: Mapping[str, Any] | None,
    generation: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
    implementation_fingerprint: str,
) -> bool:
    if not _projection_bindings_clean(
        account=account,
        source=source,
        head=head,
        generation=generation,
        projection=projection,
        implementation_fingerprint=implementation_fingerprint,
    ):
        return False
    try:
        return _decode_projection_row_payload(dict(projection or {}))[
            "normalized_account"
        ] == account
    except (CurrentDecisionProjectionError, TypeError, ValueError):
        return False
