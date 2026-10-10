import json
import re

MANAGED_DENIALS = {
    'usage_limit_reached': (402, 'Your model allowance is unavailable for this period.',
                           'Restore your allowance, then submit an explicit new job.'),
    'model_not_allowed': (403, 'This model or request path is not available for this account.',
                         'Use a qualified eligible model path, then submit an explicit new job.'),
    'funding_ended': (403, 'Funding for this account has ended.',
                     'Restore account funding, then submit an explicit new job.'),
    'request_already_admitted': (409, 'This model request was already admitted.',
                                'Inspect retained work and request status before submitting an explicit new job.'),
    'usage_unavailable': (503, 'Usage could not be checked. New model requests are paused.',
                         'Restore managed access and check account status, then submit an explicit new job.'),
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry: str = "change-input"):
        if code in MANAGED_DENIALS:
            status, message, _ = MANAGED_DENIALS[code]
            retry = 'none'
        super().__init__(message)
        self.code, self.message, self.status, self.retry = code, message, status, retry

    def as_dict(self, correlation_id: str = "") -> dict:
        result = {"code": self.code, "message": self.message, "retry": self.retry,
                  "correlationId": correlation_id}
        if self.code in MANAGED_DENIALS:
            result.update(status=self.status, retryable=False, recovery=MANAGED_DENIALS[self.code][2])
        return result


def managed_denial(value):
    """Read known codes from error objects or serialized engine errors, never document prompts."""
    def code(item, depth=0):
        if depth > 8:
            return None
        if isinstance(item, ServiceError):
            return item.code if item.code in MANAGED_DENIALS else None
        if isinstance(item, str):
            if item.lower() in MANAGED_DENIALS:
                return item.lower()
            marker = re.search(r'\[geocluster:([a-z_]+)\]', item)
            if marker and marker[1] in MANAGED_DENIALS:
                return marker[1]
            start, end = item.find('{'), item.rfind('}')
            if 0 <= start < end:
                try:
                    return code(json.loads(item[start:end + 1]), depth + 1)
                except ValueError:
                    pass
        if isinstance(item, dict):
            for key in ('code', 'type', 'error', 'message', 'data', 'cause', 'usageDenial'):
                found = code(item.get(key), depth + 1)
                if found:
                    return found
        return None

    found = code(value)
    return ServiceError(found, '') if found else None
