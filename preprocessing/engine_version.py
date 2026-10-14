"""Exact supported builds for the processor's OpenCode HTTP contract."""

SUPPORTED_VERSIONS = ('2.0.19', '2.0.19-geocluster.1', '2.0.19-geocluster.2')
VERSION_REQUIREMENT = 'Use an OpenCode build reporting one of: ' + ', '.join(SUPPORTED_VERSIONS) + '.'


def supported_version(value):
    return isinstance(value, str) and value in SUPPORTED_VERSIONS


def cli_version(output):
    """Return the supported bare version; never discard a build suffix."""
    value = output.decode('utf-8', errors='replace').strip().removeprefix('opencode v')
    return value if supported_version(value) else None
