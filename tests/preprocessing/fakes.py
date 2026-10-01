"""Explicit engine dependency for deterministic service and API fixtures."""


class FakeEngine:
    def __init__(self):
        self.descriptor = {'engineInstanceId': 'engine_test', 'directory': '/test/extraction'}
        self.available = True
        self.recovery_settled = True
        self.recovered = []
        self.on_operation = None

    def bind(self, on_operation):
        self.on_operation = on_operation

    async def ready(self, profile=None):
        return {'ready': self.available, 'engineInstanceId': 'engine_test'}

    async def recover(self, persisted_operations):
        self.recovered = list(persisted_operations)
        return {'settled': self.recovery_settled, 'operations': [
            {'id': item['id'], 'jobId': item['jobId'],
             'settlement': {'settled': self.recovery_settled},
             'cleanupState': 'deleted' if self.recovery_settled else 'pending'}
            for item in self.recovered], 'reason': 'cleanup pending'}

    async def cancel(self, job_id):
        return {'settled': True}

    async def close(self):
        pass
