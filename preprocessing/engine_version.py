"""Bounded informational versions and installation consistency, shared by all readers."""
import re

SUPPORTED_VERSIONS = ('2.0.19', '2.0.19-geocluster.1', '2.0.19-geocluster.2')


class VersionMismatch(ValueError):
    def __init__(self, evidence):
        super().__init__('The actual OpenCode versions do not match; install one consistent engine build.')
        self.evidence = evidence


def supported_version(value):
    return isinstance(value, str) and value in SUPPORTED_VERSIONS


def cli_version(output):
    """Normalize only the documented CLI prefix; preserve unknown build suffixes."""
    value = output.decode('utf-8', errors='replace').strip().removeprefix('opencode v')
    return value if valid_version(value) else None


def valid_version(value):
    return (isinstance(value, str) and len(value) <= 256
            and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9_.+-]+)?', value) is not None)


def compatibility(*, cli=None, http=None, descriptor=None, warnings=()):
    values = {'cliVersion': cli, 'httpVersion': http, 'descriptorVersion': descriptor}
    observed = [value for value in values.values() if valid_version(value)]
    notes = [v for v in warnings if isinstance(v, str) and re.fullmatch('[a-z_]{1,128}', v)][:32]
    for name, value in (('cli', cli), ('http', http), ('descriptor', descriptor)):
        if not valid_version(value):
            notes.append(name + '_version_missing' if value is None else name + '_version_invalid')
        elif not supported_version(value):
            notes.append(name + '_version_untested')
    result = {'classification': 'known-tested' if observed and all(supported_version(v) for v in observed) else 'unknown',
              **{key: value if valid_version(value) else None for key, value in values.items()},
              'warnings': list(dict.fromkeys(notes))}
    if len(set(observed)) > 1:
        result.update(classification='inconsistent', warnings=[*result['warnings'], 'version_observations_contradict'])
        raise VersionMismatch(result)
    return result
