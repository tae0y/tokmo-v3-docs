"""Closed-code canonical observation family declarations."""

from __future__ import annotations

from dataclasses import dataclass

OFFICIAL_MEMBERSHIP_OBSERVATION_FAMILY = "OfficialMembershipObservationV1"
OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY = (
    "OfficialMembershipDocumentationCaptureV1"
)


@dataclass(frozen=True, slots=True)
class CanonicalObservationFamilyV1:
    family_name: str
    owner: str
    schema_version: str
    adapter_type: str
    canonical_classification: str
    private_root_name: str


_REGISTRY = {
    OFFICIAL_MEMBERSHIP_OBSERVATION_FAMILY: CanonicalObservationFamilyV1(
        family_name=OFFICIAL_MEMBERSHIP_OBSERVATION_FAMILY,
        owner="tokmo.records.official_membership_observation_store",
        schema_version="official-membership-observation-store-v1",
        adapter_type="OfficialMembershipObservationStore",
        canonical_classification="canonical_observation_private",
        private_root_name="official-membership",
    ),
    OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY: CanonicalObservationFamilyV1(
        family_name=OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY,
        owner="tokmo.records.official_membership_capture_store",
        schema_version="official-membership-documentation-capture-store-v1",
        adapter_type="OfficialMembershipCaptureStore",
        canonical_classification="canonical_observation_private",
        private_root_name="official-membership/documentation-captures",
    ),
}


def registered_family(name: str) -> CanonicalObservationFamilyV1 | None:
    return _REGISTRY.get(name)


def require_registered_family(
    name: str,
    *,
    adapter_type: str,
) -> CanonicalObservationFamilyV1:
    family = registered_family(name)
    if family is None or family.adapter_type != adapter_type:
        raise ValueError("canonical_observation_family")
    return family
