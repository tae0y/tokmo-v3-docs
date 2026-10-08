#!/usr/bin/env python3
"""Preview a prepared test packet; --send uses the existing stock-bot sender.

Run --send only after user approval. A receipt reserves the entire packet
before transport; an existing receipt never causes an automatic resend.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from tokmo.facts.telegram_dialogue_live import send_dialogue_message


def deliver(packet, receipt_path, *, chat_id, sender):
    if hashlib.sha256(chat_id.encode()).hexdigest() != packet["recipient_sha256"]:
        raise ValueError("Recipient differs from reviewed packet")
    messages = packet["messages"]
    if not messages or any(not row["body"].startswith("[재현테스트]") for row in messages):
        raise ValueError("Every test message requires its visible label")
    if any(len(row["body"].encode("utf-16-le")) // 2 > 4096 for row in messages):
        raise ValueError("Test message exceeds Telegram size")
    receipt = {"status": "STARTED", "packet_sha256": hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "messages": []}
    receipt_path = Path(receipt_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("x") as stream:
        json.dump(receipt, stream)
    def save():
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        temporary.replace(receipt_path)
    for row in messages:
        # STARTED remains durable on process interruption: do not resend it.
        item = {"id": row["id"], "status": "STARTED"}
        receipt["messages"].append(item)
        save()
        try:
            item["provider_message_id"] = str(sender(row["body"]))
            item["status"] = "SENT"
        except Exception as exc:
            item.update(status="UNKNOWN", error_class=type(exc).__name__)
            receipt["status"] = "UNKNOWN"
            save()
            return receipt
        save()
    receipt["status"] = "SENT"
    save()
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--news-route-root", type=Path, help="Use the configured NEWS group route instead of the Stock bot")
    args = parser.parse_args(argv)
    packet = json.loads(args.packet.read_text())
    if not args.send:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
        return 0
    if args.receipt is None:
        parser.error("--send requires --receipt")
    if args.news_route_root is not None:
        from tokmo.programs.news_delivery_route import build_news_sender
        route = json.loads((args.news_route_root / "news_delivery_route.json").read_text())
        chat = route["chat_id"]
        sender = build_news_sender(args.news_route_root, None, send_dialogue_message)
    else:
        token = os.environ.get("TELEGRAM_STOCK_BOT_TOKEN") or os.environ.get("TELEGRAM_STOCK_BOT")
        chat = os.environ.get("TELEGRAM_STOCK_BOT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            parser.error("Existing stock-bot credential environment is required")
        sender = lambda body: send_dialogue_message(body_text=body, bot_token=token, chat_id=chat)
    result = deliver(packet, args.receipt, chat_id=chat, sender=sender)
    print(json.dumps({"status": result["status"], "receipt": str(args.receipt)}))
    return 0 if result["status"] == "SENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
