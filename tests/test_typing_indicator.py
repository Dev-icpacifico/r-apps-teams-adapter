"""Un fallo al enviar «escribiendo» no debe perder el mensaje (23/11)."""

import asyncio
import logging

import httpx
from microsoft_teams.api import TypingActivityInput

from typing_indicator import send_typing


class FakeCtx:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def reply(self, activity):
        self.sent.append(activity)
        if self.error is not None:
            raise self.error


def test_typing_is_sent():
    ctx = FakeCtx()
    assert asyncio.run(send_typing(ctx)) is True
    assert len(ctx.sent) == 1 and isinstance(ctx.sent[0], TypingActivityInput)


def test_connection_error_is_logged_and_swallowed(caplog):
    ctx = FakeCtx(httpx.ConnectError("tls"))
    with caplog.at_level(logging.WARNING, logger="teams-adapter"):
        assert asyncio.run(send_typing(ctx)) is False
    assert "indicador de escritura" in caplog.text and "ConnectError" in caplog.text
