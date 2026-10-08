"""Read the sole model catalog inside MODEL_ROUTING_STATE.md. No I/O or fallback.

Adapters own supported calling contracts; policy owns IDs, selectors and windows.
A selector never chains to another selector, and cannot look like a versioned ID.
"""
import json
import re

CONTRACTS = {"claude": "claude-cli", "codex": "codex-cli", "zai": "zai-chat", "agy": "agy-cli"}
BASE_WINDOWS = {"claude": {"Current session", "Current week"}, "codex": {"Session"},
                "zai": {"5시간", "주간"}, "agy": {"5시간", "주간"}}
USES = {"agent", "product", "news", "probe", "group"}
ID = re.compile(r"[a-z][a-z0-9]*(?:[-.][a-z0-9]+)+")
SELECTOR = re.compile(r"[a-z가-힣][a-z가-힣._-]*")


class RoutingError(ValueError):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_policy(text):
    """Exactly one fenced model-routing JSON object, in the current-state section."""
    try:
        sections = re.split(r"(?m)^## ", text)
        current = [s for s in sections[1:] if s.splitlines()[0].strip() == "현재 상태"]
        blocks = re.findall(r"(?m)^```model-routing\n(.*?)^```[ \t]*$", text, re.S)
        if (len(current) != 1 or len(blocks) != 1 or text.count("```model-routing") != 1
                or blocks[0] not in current[0]):
            raise ValueError("one current metadata block required")
        policy = json.loads(blocks[0], object_pairs_hook=_unique)
        if set(policy) != {"routes", "selectors"} or not policy["routes"]:
            raise ValueError("routes/selectors required")
        routes, selectors = policy["routes"], policy["selectors"]
        if not isinstance(routes, dict) or not isinstance(selectors, dict):
            raise ValueError("objects required")
        for model, row in routes.items():
            if not ID.fullmatch(model) or not isinstance(row, dict) or set(row) != {"provider", "contract", "uses", "windows"}:
                raise ValueError("route shape")
            provider = row["provider"]
            if provider not in CONTRACTS or row["contract"] != CONTRACTS[provider]:
                raise ValueError("provider/contract mismatch")
            for key, allowed in (("uses", USES), ("windows", BASE_WINDOWS[provider] | ({"Fable week"} if provider == "claude" else set()))):
                values = row[key]
                if (not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values)
                        or len(values) != len(set(values)) or not set(values) <= allowed):
                    raise ValueError(key)
            if not BASE_WINDOWS[provider] <= set(row["windows"]):
                raise ValueError("missing shared quota windows")
            if provider == "agy" and row["uses"] != ["group"]:
                raise ValueError("agy group only")
        for selector, target in selectors.items():
            if not SELECTOR.fullmatch(selector) or selector in routes or not isinstance(target, str) or target not in routes:
                raise ValueError("invalid selector target")
        return policy
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise RoutingError("ROUTING_POLICY_UNPARSEABLE", str(error)) from error


def resolve(text, selection, *, provider=None, use=None, contract=None):
    policy = parse_policy(text)
    if not isinstance(selection, str):
        raise RoutingError("ROUTE_NOT_ADMITTED")
    value = selection.strip()
    # These are documented transport qualifiers, never model-name heuristics.
    qualified = None
    if value.startswith("openai-codex/"):
        qualified, value = "codex", value[len("openai-codex/"):]
    elif ":" in value:
        qualified, value = value.split(":", 1)
    if qualified is not None:
        if qualified not in CONTRACTS or (provider is not None and provider != qualified):
            raise RoutingError("PROVIDER_MISMATCH")
        provider = qualified
    model = policy["selectors"].get(value, value)
    row = policy["routes"].get(model)
    if row is None:
        raise RoutingError("ROUTE_NOT_ADMITTED", value)
    if provider is not None and provider != row["provider"]:
        raise RoutingError("PROVIDER_MISMATCH", value)
    if (use is not None and use not in row["uses"]) or (contract is not None and contract != row["contract"]):
        raise RoutingError("ROUTE_NOT_ADMITTED", value)
    return {"model": model, **row}
