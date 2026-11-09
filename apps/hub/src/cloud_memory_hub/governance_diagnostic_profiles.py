"""Fixed file-coverage contracts; observations never grant deletion rights."""

SDK_PROFILE = "sdk-two-domains-v1"
CORE_PROFILE = "sdk-core-four-domains-v1"
_DOMAINS = {
    SDK_PROFILE: ("pipeline", "sdk"),
    CORE_PROFILE: ("core-diagnostic", "core-log", "pipeline", "sdk"),
}


def required_domains(profile: str) -> tuple[str, ...]:
    # Journal capability decoding also imports this module. Keep its error
    # import local so the shared vocabulary has no module-initialization cycle.
    if type(profile) is not str or profile not in _DOMAINS:
        from cloud_memory_hub.governance_journal import GovernanceError

        raise GovernanceError("governance_cleanup_profile_invalid")
    return _DOMAINS[profile]


def coverage_satisfies(receipt, required_profile: str) -> bool:
    """Compare strictly decoded progress with a separately verified requirement.

    The caller obtains the required profile from the actual bound journal
    snapshot. A True result is cached file coverage, not an anti-rollback proof
    or completion of business deletion and other output domains.
    """
    required = required_domains(required_profile)
    if (
        receipt is None
        or receipt.state != "verified"
        or receipt.verification is None
        or receipt.manifest is None
        or receipt.verification.profile != receipt.manifest.profile
    ):
        return False
    return set(required) <= set(required_domains(receipt.verification.profile))
