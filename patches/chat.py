from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation
from ..utils import json
from .autoresponder import Operation as Autoresponder

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    action: str
    chat_id: int
    text: str | None


class Operation(BaseOperation):
    """Чат с работодателем: `chat list [PAGE]` — список чатов, `chat data CHAT_ID` — данные чата с кнопками,
    `chat send CHAT_ID TEXT` — отправить сообщение (или текст кнопки)."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("action", choices=["list", "data", "send"])
        parser.add_argument("chat_id", type=int, nargs="?", default=0)
        parser.add_argument("text", nargs="?")

    def run(self, tool: HHApplicantTool, args: Namespace) -> None:
        chats = Autoresponder()
        chats.get_chat_url = lambda _tool: "https://chatik.hh.ru"
        if args.action == "list":
            # chat_id здесь — номер страницы
            print(json.dumps(chats.get_chats(tool, args.chat_id)))
            return None
        if args.action == "data":
            applicant_id = int(tool.api_client.get("/me")["id"])
            data = chats.get_chat_data(tool, args.chat_id, applicant_id)
            print(json.dumps(data))
            return None
        if not args.text:
            logger.error("Не задан текст сообщения")
            return 1
        chats.send_chat_message(tool, args.chat_id, args.text)
        return None
