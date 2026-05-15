"""
conftest_medilink.py — VCR configuration for Medilink API tests.

Usage:
    pytest tests/ --vcr-record=none   # run with existing cassettes only (CI)
    pytest tests/ --vcr-record=new_episodes  # record missing cassettes
    pytest tests/ --vcr-record=all    # re-record everything (use sparingly)

Never record against real Medilink in CI. Cassettes in tests/cassettes/ are
synthetic (hand-crafted) and match the real Medilink API v5 response shape.

VCR filters:
- Authorization header scrubbed (replaced with MEDILINK_TOKEN_REDACTED)
- X-Api-Key header scrubbed
- Base URL: https://api.medilink2.healthatom.com/api/v5
"""

import os
import pytest


# ---------------------------------------------------------------------------
# VCR global configuration (applied to all @pytest.mark.vcr tests)
# ---------------------------------------------------------------------------

VCR_CONFIG = {
    "cassette_library_dir": os.path.join(os.path.dirname(__file__), "cassettes"),
    "record_mode": "none",  # default: never hit real API; override with --vcr-record
    "filter_headers": [
        ("Authorization", "Bearer MEDILINK_TOKEN_REDACTED"),
        ("X-Api-Key", "MEDILINK_API_KEY_REDACTED"),
    ],
    "filter_query_parameters": [],
    "match_on": ["method", "scheme", "host", "port", "path", "query"],
    "decode_compressed_response": True,
    "serializer": "yaml",
}


@pytest.fixture(scope="module")
def vcr_config():
    return VCR_CONFIG
