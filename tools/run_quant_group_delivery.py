#!/usr/bin/env python3
"""Quant decision-card → tokmo-group delivery controller (contract §8.5 / §9.3).

Subcommands:
  preview        render the latest receipt-verified KR/US cards with cycle markers; no send
  arm            write the mode-0600 LIVE gate for ONE cycle of ONE vertical to ONE explicit chat
  send           one-attempt delivery of that cycle; the gate is disarmed by the attempt
  disable        rollback: rewrite the gate to OFF (fail-closed)
  status         budget (cycle 1 + cycle 2 + optional RCA, cap 3), gate and receipts of a vertical
  resolve-reply  map a later group reply to exact report/market/symbol (resolution only)

Identity: the already-configured Hermes Telegram identity is reused as-is.
The bot token is read from the existing Hermes env file (default
``~/.hermes/.env``); only ``TELEGRAM_BOT_TOKEN`` is retained, every other
key or comment in that file is ignored, and no credential file is ever
created, copied or modified. The token is never printed or persisted. The
recipient chat id is always an explicit ``--chat-id`` that must equal the
armed gate; the env file is not consulted for the target. No process
environment is read for secrets. No scheduler, webhook, daemon, profile,
order, account or Ledger write exists on this path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.quant_group_delivery import (
    NO_WRITES,
    admit_cycle,
    bind_markers,
    budget_status,
    resolve_group_reply,
    run_quant_group_delivery,
)
from tokmo.programs.quant_telegram_card import MARKETS
from tokmo.records.quant_group_delivery_receipts import (
    LIVE_SEND_CAP,
    RCA_CYCLE,
    TOKMO_GROUP_CHAT_ID,
    GroupBindingStore,
    activation_id_for,
    read_group_activation,
    valid_chat_id,
    valid_defect_ref,
    valid_vertical_id,
    write_group_activation,
)
from tokmo.records.quant_report_locator import locate_latest_verified_reports
from tokmo.records.quant_telegram_card_receipts import QuantTelegramCardReceiptStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
DEFAULT_REPORT_ROOT = DATA_ROOT / "ops" / "quant-g2-preview"
GATE_PATH = DATA_ROOT / "ops" / "quant-group-delivery-activation.json"
GROUP_ROOT = DATA_ROOT / "ops" / "quant-group-delivery"
DEFAULT_IDENTITY_PATH = (
    Path.home() / ".hermes" / ".env"
)  # existing Hermes identity, reused
IDENTITY_KEY = "TELEGRAM_BOT_TOKEN"
EXIT_OK, EXIT_REFUSED, EXIT_NOT_SENT = 0, 2, 3
_CARD_TOOL = Path(__file__).resolve().with_name("run_quant_telegram_card.py")


def _card_tool():
    spec = importlib.util.spec_from_file_location("run_quant_telegram_card", _CARD_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_group_identity(path: Path, *, uid: int) -> str:
    """Bot token from the existing Hermes env file; fail closed.

    The file is an owner-only regular file (no group/other permission bits)
    that may hold many unrelated keys and comments: only ``TELEGRAM_BOT_TOKEN``
    is retained, exactly once, non-empty. Nothing is written back."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("identity file unreadable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_uid != uid
    ):
        raise ValueError("unsafe identity file")
    token = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue  # comments, blanks and unrelated lines are ignored
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key != IDENTITY_KEY:
            continue  # unrelated key: never read, never retained
        if token is not None:
            raise ValueError("duplicate bot token key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            raise ValueError("empty bot token")
        token = value
    if token is None:
        raise ValueError("missing bot token")
    return token


def _cards_by_market(report_root: Path) -> dict:
    located = locate_latest_verified_reports(report_root)
    return {
        market: (
            ()
            if item is None
            else tuple((card.symbol, card.name) for card in item.report.cards)
        )
        for market, item in located.items()
    }


def _stores(group_root: Path):
    return (
        QuantTelegramCardReceiptStore(Path(group_root) / "receipts"),
        GroupBindingStore(Path(group_root) / "bindings"),
    )


def main(
    argv=None,
    *,
    clock=None,
    sender_factory=None,
    gate_path=GATE_PATH,
    group_root=GROUP_ROOT,
    uid=None,
    conditional_fetch=None,
    **paths,
) -> int:
    parser = argparse.ArgumentParser(
        description="Quant decision-card → tokmo-group delivery controller"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview")
    preview.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    preview.add_argument("--cycle", type=int, default=1)
    preview.add_argument(
        "--text", action="store_true", help="print message parts as plain text"
    )
    preview.add_argument(
        "--conditional-live",
        action="store_true",
        help="perform the bounded OPEN conditional-order read (one page per mapped account, retry 0)",
    )
    arm = sub.add_parser("arm")
    arm.add_argument("--vertical-id", required=True)
    arm.add_argument("--cycle", type=int, required=True)
    arm.add_argument(
        "--chat-id",
        required=True,
        help=f"explicit target chat (tokmo group: {TOKMO_GROUP_CHAT_ID})",
    )
    arm.add_argument("--expires-in-minutes", type=int, default=30)
    arm.add_argument(
        "--rca-defect-ref",
        default=None,
        help="documented defect reference; required for cycle 3",
    )
    arm.add_argument(
        "--same-report-ok",
        action="store_true",
        help="cycle may re-render a report already SENT/FAILED in this vertical; UNKNOWN never",
    )
    send = sub.add_parser("send")
    send.add_argument("--vertical-id", required=True)
    send.add_argument("--cycle", type=int, required=True)
    send.add_argument("--chat-id", required=True)
    send.add_argument(
        "--telegram-env",
        type=Path,
        default=DEFAULT_IDENTITY_PATH,
        help="existing Hermes env file; only TELEGRAM_BOT_TOKEN is read",
    )
    send.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    send.add_argument(
        "--markets", default="KR,US", help="comma-separated subset of KR,US"
    )
    send.add_argument(
        "--expect-report-sha256",
        action="append",
        default=[],
        metavar="MARKET=SHA256",
        help="refuse unless the located report digest matches",
    )
    sub.add_parser("disable")
    status = sub.add_parser("status")
    status.add_argument("--vertical-id", required=True)
    resolve = sub.add_parser(
        "resolve-reply", help="resolution only; the reply is NOT processed"
    )
    resolve.add_argument("--chat-id", required=True)
    resolve.add_argument("--reply-to-message-id", type=int, default=None)
    resolve.add_argument("--text", required=True)
    args = parser.parse_args(argv)

    now = (clock or (lambda: datetime.now(UTC)))()
    uid = os.getuid() if uid is None else uid
    emit = lambda payload: print(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    card_tool = _card_tool()
    provider_calls = None

    def conditional_seam(live: bool):
        """Injected fetch wins; otherwise the official Toss read adapter only when
        a live read is wanted; any seam construction failure leaves the read
        unperformed (cards say 조건주문 확인 불가 — 조회 미실행) and is reported."""
        nonlocal provider_calls
        if conditional_fetch is not None:
            return conditional_fetch
        if not live:
            return None
        try:
            fetch, provider_calls = card_tool.build_live_conditional_fetch(
                clock=lambda: now
            )
        except (
            Exception
        ) as error:  # credential/config absent: fail closed, never crash the send
            provider_calls = {"seam": "unavailable", "error": type(error).__name__}
            return None
        return fetch

    if args.command == "preview":
        if args.cycle < 1 or args.cycle > LIVE_SEND_CAP:
            emit({"mode": "preview", "sent": 0, "off": "invalid_cycle"})
            return EXIT_REFUSED
        messages, summary = card_tool.render(
            args.report_root,
            now=now,
            conditional_fetch=conditional_seam(args.conditional_live),
            **paths,
        )
        try:
            marked, parts = bind_markers(
                messages, _cards_by_market(args.report_root), cycle=args.cycle
            )
        except ValueError as error:
            emit({"mode": "preview", "sent": 0, "off": str(error), "reports": summary})
            return EXIT_REFUSED
        if args.text:
            for message in marked:
                for index, part in enumerate(message.parts, 1):
                    print(
                        f"===== {message.market} part {index}/{len(message.parts)} ====="
                    )
                    print(part)
                    print()
        emit(
            {
                "mode": "preview",
                "sent": 0,
                "cycle": args.cycle,
                "reports": summary,
                "provider_calls": provider_calls,
                "markers": {key: value["marker"] for key, value in parts.items()},
                "symbols": {key: value["symbols"] for key, value in parts.items()},
                "messages": [card_tool._message_summary(message) for message in marked],
            }
        )
        return EXIT_OK

    if args.command == "arm":
        if (
            not valid_vertical_id(args.vertical_id)
            or not valid_chat_id(args.chat_id)
            or args.cycle < 1
            or args.cycle > LIVE_SEND_CAP
            or args.expires_in_minutes < 1
            or args.expires_in_minutes > 24 * 60
        ):
            emit({"armed": False, "reason": "invalid_parameters"})
            return EXIT_REFUSED
        if args.cycle == RCA_CYCLE and not valid_defect_ref(args.rca_defect_ref):
            emit({"armed": False, "reason": "rca_defect_missing"})
            return EXIT_REFUSED
        if args.cycle != RCA_CYCLE and args.rca_defect_ref is not None:
            emit({"armed": False, "reason": "invalid_parameters"})
            return EXIT_REFUSED
        receipts, bindings = _stores(group_root)
        refusal = admit_cycle(
            receipts,
            vertical_id=args.vertical_id,
            cycle=args.cycle,
            rca_defect_ref=args.rca_defect_ref,
        )
        if refusal is not None:
            emit(
                {
                    "armed": False,
                    "reason": refusal,
                    "budget": budget_status(
                        receipts, bindings, vertical_id=args.vertical_id
                    ),
                }
            )
            return EXIT_REFUSED
        expires = now + timedelta(minutes=args.expires_in_minutes)
        write_group_activation(
            gate_path,
            vertical_id=args.vertical_id,
            cycle=args.cycle,
            chat_id=args.chat_id,
            mode="LIVE",
            expires_at=expires,
            rca_defect_ref=args.rca_defect_ref,
            same_report_ok=args.same_report_ok,
        )
        emit(
            {
                "armed": True,
                "activation_id": activation_id_for(args.vertical_id, args.cycle),
                "cycle": args.cycle,
                "chat_id": args.chat_id,
                "expires_at": expires.isoformat(),
                "gate_path": str(gate_path),
            }
        )
        return EXIT_OK

    if args.command == "disable":
        current = read_group_activation(gate_path, now=now)
        write_group_activation(
            gate_path,
            vertical_id=current.get("vertical_id") or "rollback-off",
            cycle=1,
            chat_id=current.get("chat_id") or TOKMO_GROUP_CHAT_ID,
            mode="OFF",
            expires_at=now + timedelta(days=3650),
        )
        emit(
            {
                "disabled": True,
                "gate_path": str(gate_path),
                "readback": read_group_activation(gate_path, now=now)["mode"],
            }
        )
        return EXIT_OK

    if args.command == "status":
        if not valid_vertical_id(args.vertical_id):
            emit({"vertical_id": args.vertical_id, "off": "invalid_vertical_id"})
            return EXIT_REFUSED
        receipts, bindings = _stores(group_root)
        ids = [
            activation_id_for(args.vertical_id, cycle)
            for cycle in range(1, LIVE_SEND_CAP + 1)
        ]
        emit(
            {
                "vertical_id": args.vertical_id,
                "budget": budget_status(
                    receipts, bindings, vertical_id=args.vertical_id
                ),
                "gate": read_group_activation(gate_path, now=now),
                "receipts": {
                    activation_id: receipts.read(activation_id) for activation_id in ids
                },
                "bindings": {
                    activation_id: bindings.read(activation_id) for activation_id in ids
                },
            }
        )
        return EXIT_OK

    if args.command == "resolve-reply":
        receipts, bindings = _stores(group_root)
        binding = resolve_group_reply(
            receipts=receipts,
            bindings=bindings,
            chat_id=args.chat_id,
            reply_to_message_id=args.reply_to_message_id,
            text=args.text,
        )
        payload = {
            name: getattr(binding, name) for name in binding.__dataclass_fields__
        }
        payload["writes"] = dict(NO_WRITES)
        payload["candidates"] = [list(item) for item in binding.candidates]
        emit(
            {
                "binding": payload,
                "processed": False,
                "note": "resolution only — reply processing (preview/approval/write) is not implemented",
            }
        )
        return (
            EXIT_OK
            if binding.status in ("bound", "symbol_missing", "symbol_ambiguous")
            else EXIT_REFUSED
        )

    # send
    activation = read_group_activation(gate_path, now=now)
    if activation["mode"] != "LIVE":
        emit(
            {
                "sent": 0,
                "off": activation.get("reason") or "gate_off",
                "gate": activation,
            }
        )
        return EXIT_REFUSED
    if (
        activation["vertical_id"] != args.vertical_id
        or activation["cycle"] != args.cycle
    ):
        emit({"sent": 0, "off": "activation_mismatch", "gate": activation})
        return EXIT_REFUSED
    if not valid_chat_id(args.chat_id) or args.chat_id != activation["chat_id"]:
        emit({"sent": 0, "off": "chat_mismatch"})
        return EXIT_REFUSED
    try:
        token = load_group_identity(args.telegram_env, uid=uid)
    except ValueError:
        emit({"sent": 0, "off": "identity_unbound"})
        return EXIT_REFUSED
    markets = tuple(
        dict.fromkeys(item.strip() for item in args.markets.split(",") if item.strip())
    )
    if not markets or any(market not in MARKETS for market in markets):
        emit({"sent": 0, "off": "invalid_markets"})
        return EXIT_REFUSED
    expected = {}
    for item in args.expect_report_sha256:
        market, separator, digest = item.partition("=")
        if separator != "=" or market not in markets or len(digest) != 64:
            emit({"sent": 0, "off": "invalid_expected_digest"})
            return EXIT_REFUSED
        expected[market] = digest.lower()
    messages, summary = card_tool.render(
        args.report_root, now=now, conditional_fetch=conditional_seam(True), **paths
    )
    messages = tuple(message for message in messages if message.market in markets)
    for message in messages:
        if (
            message.market in expected
            and message.report_sha256 != expected[message.market]
        ):
            emit(
                {
                    "sent": 0,
                    "off": f"report_digest_mismatch:{message.market}",
                    "located_report_sha256": message.report_sha256,
                    "reports": summary,
                }
            )
            return EXIT_REFUSED
    # live sender = the card tool's pre-entity sender (monospace return table via
    # a Telegram ``pre`` entity over the telegram_dialogue_live transport)
    telegram_send = (
        card_tool.pre_entity_sender if sender_factory is None else sender_factory
    )(token, args.chat_id)
    receipts, bindings = _stores(group_root)
    result = run_quant_group_delivery(
        activation=activation,
        chat_id=args.chat_id,
        receipts=receipts,
        bindings=bindings,
        messages=messages,
        cards_by_market=_cards_by_market(args.report_root),
        telegram_send=telegram_send,
        now=now,
    )
    gate_after = activation["mode"]
    if result["reserved"] == 1:
        # the gate authorizes exactly one attempt: disarm so nothing recurs
        write_group_activation(
            gate_path,
            vertical_id=activation["vertical_id"],
            cycle=1,
            chat_id=activation["chat_id"],
            mode="OFF",
            expires_at=now + timedelta(days=3650),
        )
        gate_after = read_group_activation(gate_path, now=now)["mode"]
    activation_id = result["activation_id"]
    receipt = receipts.read(activation_id) if activation_id else None
    emit(
        {
            "activation_id": activation_id,
            "cycle": args.cycle,
            "chat_id": args.chat_id,
            "markets": list(markets),
            "off": result["off"],
            "result": result,
            "reports": summary,
            "provider_calls": provider_calls,
            "receipt_status": None if receipt is None else receipt["status"],
            "receipt_path": None
            if activation_id is None
            else str(receipts.path(activation_id)),
            "binding_path": None
            if activation_id is None
            else str(bindings.path(activation_id)),
            "gate_after": gate_after,
            "budget": budget_status(
                receipts, bindings, vertical_id=activation["vertical_id"]
            ),
            "messages": [card_tool._message_summary(message) for message in messages],
        }
    )
    if result["off"] is not None:
        return EXIT_REFUSED
    return (
        EXIT_OK
        if result["failed"] == 0 and result["unknown"] == 0 and result["skipped"] == 0
        else EXIT_NOT_SENT
    )


if __name__ == "__main__":
    raise SystemExit(main())
