from fastapi import Request


async def require_auth(request: Request) -> None:
    """No-op auth dependency for v1 (localhost only).

    v2: validate Bearer token from Authorization header.
    """
    pass
