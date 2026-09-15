"""User-owned model access: HOW an already-selected binding connects.

This package answers exactly one question: given an
already-selected ``ExecutionBinding`` (WHAT), which ``ProviderConnection``
(HOW) authenticates and reaches the provider/account/backend.  It never
selects, ranks or filters bindings: routing, eligibility, quota policy
and tier decisions remain owned by ``control_plane`` (C07); Attempt
lifecycle by C11-D; tool/security authority by ``sandbox`` (C08/C11-C),
whose credential-reference contract this package consumes without
re-implementing.

Access resolution happens AFTER selection::

    control_plane -> ExecutionBinding -> model_access -> ProviderConnection
                        (selects)        (resolves HOW)     (durable access
                                                            identity, refs
                                                            only)
"""

from saberops.model_access.connections import (
    ACCESS_RESOLUTION_REASONS,
    ACCOUNT_BACKENDS,
    CONNECTION_TEST_REASONS,
    AccessProtocol,
    AccessRegistry,
    AccessResolutionError,
    AccessStore,
    AccessStoreError,
    AccountBackend,
    AuthMode,
    ConnectionTestReason,
    ConnectionTestResult,
    DiscoveryState,
    ProviderConnection,
    ResolvedAccess,
    TriState,
    access_registry_from_mapping,
    build_access_registry,
    classify_api_probe,
    connection_from_mapping,
    default_account_connections,
    list_presets,
    preset_connection,
    probe_account_backend,
    resolve_access,
)
from saberops.model_access.discovery import (
    DiscoveredModel,
    ExecutionSupport,
    ModelCatalog,
    ModelCatalogStore,
    ModelCatalogStoreError,
    ModelDiscoveryResult,
    ModelListingError,
    ModelSource,
    apply_discovery_result,
    build_model_catalog,
    discover_connection_models,
    discovered_model_from_mapping,
    discovery_state_for,
    model_catalog_from_mapping,
    replace_connection_models,
)
from saberops.model_access.readiness import (
    READINESS_REASONS,
    ProviderReadinessEvidence,
    ReadinessCatalog,
    ReadinessEvidenceSource,
    ReadinessStore,
    ReadinessStoreError,
    build_readiness_catalog,
    derive_dispatch_readiness,
    readiness_catalog_from_mapping,
    readiness_evidence_from_mapping,
)
from saberops.model_access.readiness_service import (
    DEFAULT_READINESS_MAX_AGE,
    ReadinessAdmission,
    ReadinessService,
)

__all__ = [
    "ACCESS_RESOLUTION_REASONS",
    "ACCOUNT_BACKENDS",
    "CONNECTION_TEST_REASONS",
    "AccessProtocol",
    "AccessRegistry",
    "AccessResolutionError",
    "AccessStore",
    "AccessStoreError",
    "AccountBackend",
    "AuthMode",
    "ConnectionTestReason",
    "ConnectionTestResult",
    "DiscoveredModel",
    "DiscoveryState",
    "ExecutionSupport",
    "ModelCatalog",
    "ModelCatalogStore",
    "ModelCatalogStoreError",
    "ModelDiscoveryResult",
    "ModelListingError",
    "ModelSource",
    "ProviderReadinessEvidence",
    "ReadinessCatalog",
    "ReadinessEvidenceSource",
    "ReadinessStore",
    "ReadinessStoreError",
    "ReadinessAdmission",
    "ReadinessService",
    "DEFAULT_READINESS_MAX_AGE",
    "READINESS_REASONS",
    "ProviderConnection",
    "ResolvedAccess",
    "TriState",
    "access_registry_from_mapping",
    "apply_discovery_result",
    "build_access_registry",
    "build_model_catalog",
    "build_readiness_catalog",
    "classify_api_probe",
    "connection_from_mapping",
    "default_account_connections",
    "discover_connection_models",
    "discovered_model_from_mapping",
    "discovery_state_for",
    "list_presets",
    "model_catalog_from_mapping",
    "preset_connection",
    "probe_account_backend",
    "replace_connection_models",
    "derive_dispatch_readiness",
    "readiness_catalog_from_mapping",
    "readiness_evidence_from_mapping",
    "resolve_access",
]
