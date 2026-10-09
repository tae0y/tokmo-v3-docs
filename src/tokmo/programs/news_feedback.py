"""Canonical alert-local NEWS feedback adapter; never polls Telegram."""

import re


def build_news_callback_handler(store, ops_kernel_reader, *, now):
    def handler(update):
        parts = str(update.get("data", "")).split(":")
        if (
            len(parts) != 3
            or parts[0] != "n1"
            or parts[1] not in {"u", "d"}
            or re.fullmatch(r"[0-9a-f]{32}", parts[2]) is None
        ):
            return "IGNORED"
        revision = store.revision_by_callback(parts[2])
        if revision is None:
            return "IGNORED"
        outbox = ops_kernel_reader("nal-" + revision["alert_revision_id"][:24])
        provider_message_id = None if outbox is None else outbox.provider_message_id
        if provider_message_id != str(update.get("message_id")):
            return "IGNORED"
        event = {
            **revision,
            "provider_update_id": int(update["update_id"]),
            "provider_message_id": provider_message_id,
            "reaction": "HELPFUL" if parts[1] == "u" else "NOT_HELPFUL",
        }
        replay = store.append_reaction(event, now=now())
        return "DUPLICATE" if replay.value == "DUPLICATE" else "RECORDED"

    return handler


def run_news_feedback_reconcile(dialogue_store, handler):
    return tuple(handler(update) for update in dialogue_store.list_news_inbound())
