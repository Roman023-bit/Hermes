"""Host-level alerting for Hermes and Knowledge Factory operations.

This package deliberately lives outside the agent loop.  It has no model
tools and never needs the gateway or the Hermes container to be running.
"""
