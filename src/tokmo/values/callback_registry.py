"""Central, collision-free Telegram callback prefix registry."""

import re

NEWS_FEEDBACK_PREFIX = "n1:"
TRADE_QUESTION_PREFIX = "q1:"
TRADE_PREVIEW_PREFIX = "p1:"
# Receipt bubbles carry exactly one 수정 button. The prefix is distinct from
# the abolished preview grammar so an old in-flight preview bubble keeps its
# own meaning instead of being reinterpreted by the new receipt handler.
TRADE_RECEIPT_PREFIX = "r1:"
CB_QUESTION_YES = TRADE_QUESTION_PREFIX + "yes"
CB_QUESTION_NONE = TRADE_QUESTION_PREFIX + "none"
CB_QUESTION_INPUT = TRADE_QUESTION_PREFIX + "input"
CB_PREVIEW_OK = TRADE_PREVIEW_PREFIX + "ok"
CB_PREVIEW_CANCEL = TRADE_PREVIEW_PREFIX + "cancel"
CB_PREVIEW_EDIT = TRADE_PREVIEW_PREFIX + "edit"
CB_RECEIPT_EDIT = TRADE_RECEIPT_PREFIX + "edit"


class CallbackRegistryError(ValueError):
    pass


class CallbackRegistry:
    def __init__(self, prefixes):
        self.prefixes = tuple(prefixes)
        if any(
            a != b and (a.startswith(b) or b.startswith(a))
            for a in self.prefixes
            for b in self.prefixes
        ):
            raise CallbackRegistryError("callback prefixes overlap")

    def owner(self, data):
        encoded = data.encode("utf-8")
        if len(encoded) > 64:
            raise CallbackRegistryError("callback data exceeds 64 bytes")
        return next(
            (prefix for prefix in self.prefixes if data.startswith(prefix)), None
        )


def news_feedback_callback(ref, *, useful):
    data = f"{NEWS_FEEDBACK_PREFIX}{'u' if useful else 'd'}:{ref}"
    if len(data.encode("utf-8")) > 64 or re.fullmatch(r"[0-9a-f]{32}", ref) is None:
        raise CallbackRegistryError("invalid NEWS callback data")
    return data


CALLBACK_REGISTRY = CallbackRegistry(
    (
        TRADE_QUESTION_PREFIX,
        TRADE_PREVIEW_PREFIX,
        TRADE_RECEIPT_PREFIX,
        NEWS_FEEDBACK_PREFIX,
    )
)
