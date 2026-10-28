import logging
import time

logger = logging.getLogger("ai-receptionist")


class LoggerMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.time()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                duration_ms = round((time.time() - start) * 1000, 2)
                logger.info(
                    "%s %s -> %s (%sms)",
                    scope.get("method"),
                    scope.get("path"),
                    message.get("status"),
                    duration_ms,
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)
