class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry: str = "change-input"):
        super().__init__(message)
        self.code, self.message, self.status, self.retry = code, message, status, retry

    def as_dict(self, correlation_id: str = "") -> dict:
        return {"code": self.code, "message": self.message, "retry": self.retry,
                "correlationId": correlation_id}
