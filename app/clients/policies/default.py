from app.clients.base_policy import BaseClientPolicy


class DefaultClientPolicy(BaseClientPolicy):
    """No client-specific transformations; standard backend contract applies."""
