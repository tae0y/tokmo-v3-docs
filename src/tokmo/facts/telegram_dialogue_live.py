"""G3 trade-dialogue live Telegram transport (activation SDS v0.2 §4).

Registered in LIVE_TRANSPORT_MODULES; only composition may import it. This
is a NEW opener contract, distinct from both the shadow lifecycle sender
(which discards the response and swallows every failure) and the pension
reminder transport: the sendMessage response body is parsed and the
Telegram ``message_id`` is returned so the outbox can bind it; a network
timeout raises ``TimeoutError`` (the outbox settles UNKNOWN, never
resent); every other failure — including 429/5xx, which the SDS declares
accepted loss — propagates (the outbox settles FAILED, no retry). Nothing
is swallowed. Uses stdlib HTTPS only; token and chat id arrive as sealed
arguments and are never logged or persisted.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request

SEND_TIMEOUT_SECONDS = 10.0
UPDATES_TIMEOUT_SECONDS = 10.0
ANSWER_TIMEOUT_SECONDS = 5.0
UPDATES_LIMIT = 100


class DialogueSendRejected(RuntimeError):
    """Telegram answered but rejected the send (ok != true)."""


class DialogueUpdatesRejected(RuntimeError):
    """Telegram getUpdates response was rejected or malformed."""


def open_send_message(url: str, body: bytes, timeout: float) -> bytes:
    """Default HTTPS opener: POST the form body and RETURN the response."""
    context = ssl.create_default_context()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(
            request, timeout=timeout, context=context) as response:
        return response.read()


def open_get_updates(url: str, timeout: float) -> bytes:
    """Default HTTPS opener: one non-long-polling getUpdates request."""
    context = ssl.create_default_context()
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(
            request, timeout=timeout, context=context) as response:
        return response.read()


def fetch_dialogue_updates(*, offset: int, bot_token: str, chat_id: str,
                           opener=None, limit: int = UPDATES_LIMIT,
                           timeout: float = UPDATES_TIMEOUT_SECONDS) -> tuple:
    """Fetch once; retain text only from the configured private chat AND
    the configured sender identity (message.from.id), exactly like the
    callback path — a private chat match alone never authorizes.

    Rejected updates retain only their numeric update identity so the durable
    cursor can advance without storing untrusted text or producing a reply.
    """
    if type(offset) is not int or offset < 0 or not bot_token or not chat_id:
        raise ValueError("missing or invalid required parameters")
    if type(limit) is not int or not 1 <= limit <= UPDATES_LIMIT:
        raise ValueError("invalid update limit")
    query = urllib.parse.urlencode({
        "offset": offset,
        "limit": limit,
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"],
                                      separators=(",", ":")),
    })
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?{query}"
    raw = (open_get_updates if opener is None else opener)(url, timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DialogueUpdatesRejected("telegram updates malformed") from exc
    if type(payload) is not dict or payload.get("ok") is not True \
            or type(payload.get("result")) is not list:
        raise DialogueUpdatesRejected("telegram updates rejected")
    normalized = []
    for item in payload["result"]:
        if type(item) is not dict or type(item.get("update_id")) is not int:
            raise DialogueUpdatesRejected("telegram update identity malformed")
        update_id = item["update_id"]
        callback = item.get("callback_query")
        if type(callback) is dict:
            normalized.append(
                _normalize_callback(callback, update_id, chat_id))
            continue
        message = item.get("message")
        sender = message.get("from") if type(message) is dict else None
        authorized = (
            type(message) is dict
            and type(message.get("chat")) is dict
            and message["chat"].get("type") == "private"
            and str(message["chat"].get("id")) == str(chat_id)
            and type(sender) is dict
            and str(sender.get("id")) == str(chat_id)
            and type(message.get("message_id")) is int
            and type(message.get("text")) is str
        )
        if not authorized:
            normalized.append({"update_id": update_id, "ignored": True})
            continue
        reply = message.get("reply_to_message")
        reply_id = (reply.get("message_id") if type(reply) is dict else None)
        if reply_id is not None and type(reply_id) is not int:
            normalized.append({"update_id": update_id, "ignored": True})
            continue
        normalized.append({
            "update_id": update_id,
            "message_id": message["message_id"],
            "reply_to_message_id": reply_id,
            "text": message["text"],
        })
    return tuple(normalized)


def _normalize_callback(callback: dict, update_id: int, chat_id: str) -> dict:
    """Retain only short deterministic callback identity from the
    configured private chat/user; anything else keeps numeric identity
    only so the cursor can advance."""
    message = callback.get("message")
    sender = callback.get("from")
    authorized = (
        type(callback.get("id")) is str and callback["id"]
        and type(sender) is dict
        and str(sender.get("id")) == str(chat_id)
        and type(message) is dict
        and type(message.get("chat")) is dict
        and message["chat"].get("type") == "private"
        and str(message["chat"].get("id")) == str(chat_id)
        and type(message.get("message_id")) is int
        and type(callback.get("data")) is str and callback["data"]
    )
    if not authorized:
        return {"update_id": update_id, "ignored": True}
    return {
        "update_id": update_id,
        "callback": True,
        "callback_query_id": callback["id"],
        "message_id": message["message_id"],
        "data": callback["data"],
    }


def send_dialogue_message(*, body_text: str, bot_token: str, chat_id: str,
                          reply_markup: "dict | None" = None, entities=None, protect_content: bool | None = None,
                          opener=None,
                          timeout: float = SEND_TIMEOUT_SECONDS) -> int:
    """Send one dialogue message; return the provider message_id.

    ``reply_markup`` (an inline keyboard mapping) and ``entities`` (a
    sequence of Telegram MessageEntity mappings with UTF-16 offsets, e.g.
    one ``pre`` block for a monospace table) are serialized into the same
    deterministic form body; each is omitted entirely when None so the
    legacy sendMessage contract is byte-identical. No parse mode is ever
    set: the text is always posted verbatim.
    """
    if not body_text or not bot_token or not chat_id:
        raise ValueError("missing required parameters")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    fields = {"chat_id": chat_id, "text": body_text}
    if reply_markup is not None:
        fields["reply_markup"] = json.dumps(
            reply_markup, ensure_ascii=False, separators=(",", ":"))
    if entities is not None:
        fields["entities"] = json.dumps(
            list(entities), ensure_ascii=False, separators=(",", ":"))
    if protect_content is not None:
        fields["protect_content"] = json.dumps(protect_content)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    raw = (open_send_message if opener is None else opener)(
        url, body, timeout)
    result = json.loads(raw.decode("utf-8"))
    if result.get("ok") is not True:
        raise DialogueSendRejected("telegram send rejected")
    return int(result["result"]["message_id"])


def answer_dialogue_callback(*, callback_query_id: str, bot_token: str,
                             opener=None,
                             timeout: float = ANSWER_TIMEOUT_SECONDS) -> bool:
    """Bounded answerCallbackQuery ack so the Telegram spinner clears.

    Deliberately the opposite failure contract of the senders above:
    every failure is swallowed into ``False`` — an ack can never abort
    inbound processing, trigger a canonical append, or surface a raw
    provider error/token to any caller.
    """
    if type(callback_query_id) is not str or not callback_query_id \
            or not bot_token:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    body = urllib.parse.urlencode(
        {"callback_query_id": callback_query_id}).encode("utf-8")
    try:
        raw = (open_send_message if opener is None else opener)(
            url, body, timeout)
        return json.loads(raw.decode("utf-8")).get("ok") is True
    except Exception:
        return False
