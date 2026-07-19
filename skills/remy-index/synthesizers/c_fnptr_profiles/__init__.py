"""Project-type profiles for the C function-pointer dispatch synthesizer.

Each profile tunes project-specific behavior over the generic engine. Add a new
file per project type (e.g. linux.py, redis.py) and register it in PROFILES.
"""

from .tee import TEE_PROFILE

PROFILES = {
    "tee": TEE_PROFILE,
}

DEFAULT_PROFILE = "tee"


def get_profile(name=None):
    """Return the named profile, falling back to the default."""
    return PROFILES.get(name or DEFAULT_PROFILE, TEE_PROFILE)
