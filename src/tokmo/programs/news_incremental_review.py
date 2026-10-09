"""Small views over existing NEWS records; no extra model, queue or source writes."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from tokmo.programs.news_context_efficiency import context_basis, followup_scope
from tokmo.records.news_decision_store import encoded


def _pointer(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _identity(row):
    if not isinstance(row, dict):
        return None
    if row.get("id"):
        return ("id", row["id"])
    if row.get("key"):
        return ("key", row.get("account"), row.get("scope"), row["key"])
    # ETF context identifies an instrument by market/symbol, not id/key.
    # Only a unique current row is referenced by prior_delta; every changed
    # historical field and timestamp remains exactly reconstructable.
    if (
        isinstance(row.get("market"), str)
        and isinstance(row.get("symbol"), str)
        and row["market"]
        and row["symbol"]
    ):
        return ("market_symbol", row["market"], row["symbol"])
    return None


def prior_delta(prior, current, path=""):
    """Lossless old-value view. References address the CURRENT context by JSON pointer.

    Keep an old value unless an exact reconstructable representation is smaller.
    No numeric rounding, text summarization, timestamp removal or voice change.
    """
    reference = {"same_context_pointer": path}
    if prior == current:
        candidate = reference
    elif isinstance(prior, dict) and isinstance(current, dict):
        candidate = {
            k: prior_delta(v, current[k], _pointer(path, k))
            if k in current
            else deepcopy(v)
            for k, v in prior.items()
        }
    elif isinstance(prior, list) and isinstance(current, list):
        indexes = {}
        for i, row in enumerate(current):
            key = _identity(row)
            if key is not None:
                indexes.setdefault(key, []).append(i)
        candidate = []
        for row in prior:
            matches = indexes.get(_identity(row), [])
            if len(matches) == 1:
                i = matches[0]
                candidate.append(prior_delta(row, current[i], _pointer(path, i)))
            else:
                candidate.append(deepcopy(row))
    else:
        return deepcopy(prior)
    return (
        candidate if len(encoded(candidate)) < len(encoded(prior)) else deepcopy(prior)
    )


def expand_prior_delta(value, current):
    """Reconstruct archived context for replay checks; never reads files or URLs."""
    if isinstance(value, dict):
        if set(value) == {"same_context_pointer"}:
            target = current
            path = value["same_context_pointer"]
            if path:
                if not isinstance(path, str) or not path.startswith("/"):
                    raise ValueError("invalid context pointer")
                for part in path[1:].split("/"):
                    key = part.replace("~1", "/").replace("~0", "~")
                    target = (
                        target[int(key)] if isinstance(target, list) else target[key]
                    )
            return deepcopy(target)
        return {k: expand_prior_delta(v, current) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_prior_delta(v, current) for v in value]
    return deepcopy(value)


def portfolio_keys(context):
    return {
        r["key"]
        for name in ("holdings", "instruments")
        for r in context.get(name, [])
        if isinstance(r, dict) and isinstance(r.get("key"), str)
    }


def review_dependencies(value, context):
    """Invalid/unknown scope broadens review; auxiliary output never vetoes a warning."""
    broad = dict(all_portfolio=True, keys=[])
    if not isinstance(value, dict) or type(value.get("all_portfolio")) is not bool:
        return broad
    if value["all_portfolio"]:
        return broad
    keys = value.get("keys")
    if (
        not isinstance(keys, list)
        or len(keys) > 100
        or any(not isinstance(k, str) or k not in portfolio_keys(context) for k in keys)
    ):
        return broad
    return dict(all_portfolio=False, keys=sorted(set(keys)))


def _mentioned_keys(value, known):
    if isinstance(value, str):
        return {value} & known
    if isinstance(value, dict):
        return set().union(
            *(_mentioned_keys(v, known) for v in value.values()),
            *({k} & known for k in value),
        )
    if isinstance(value, list):
        return set().union(*(_mentioned_keys(v, known) for v in value))
    return set()


def _etf_keys(row, known):
    keys = _mentioned_keys(row, known)
    if isinstance(row, dict):
        # KIS membership carries local codes, not canonical keys. A matching
        # code broadens review only; it never proves weights or exact exposure.
        codes = {row.get("symbol")}
        codes.update(
            r.get("code")
            for r in (row.get("components") or {}).get("rows", [])
            if isinstance(r, dict)
        )
        keys.update(k for k in known if k.split(":", 1)[-1] in codes)
    return keys


def dependent_keys(event, context):
    dependency = review_dependencies(event.get("review_dependencies"), context)
    known = portfolio_keys(context)
    if dependency["all_portfolio"]:
        return known
    keys = set(dependency["keys"])
    keys.update(
        k for k in event.get("decision", {}).get("subject_keys", []) if k in known
    )
    scope = followup_scope(event.get("followup_scope"), context)
    for c in (context.get("investment_followup") or {}).get("choices", []):
        if c.get("id") in scope["choice_ids"]:
            keys.update(c.get("keys", []))
    # Only actual structured ETF links, not a model's remembered constituents.
    etfs = context.get("etf_context") or []
    rows = (
        etfs
        if isinstance(etfs, list)
        else list(etfs.values())
        if isinstance(etfs, dict)
        else []
    )
    groups = [_etf_keys(row, known) for row in rows]
    for _ in range(len(groups)):
        expanded = keys | set().union(*(g for g in groups if g & keys))
        if expanded == keys:
            break
        keys = expanded
    return keys


def dependency_context(event, context):
    """Versions of relevant data, plus a small universe/health guard for new exposure.

    Source read timestamps are excluded by the established context_basis policy.
    New/removed assets still trigger exploration; unrelated quantity changes do not.
    """
    cleaned = context_basis(context)
    if review_dependencies(event.get("review_dependencies"), context)["all_portfolio"]:
        if isinstance(cleaned.get("investment_followup"), dict):
            cleaned["investment_followup"].pop("delivered_reviews", None)
        return cleaned
    keys = dependent_keys(event, context)
    result = {
        k: deepcopy(v)
        for k, v in cleaned.items()
        if k not in {"holdings", "instruments", "investment_followup", "etf_context"}
    }
    result["inventory"] = sorted(
        {
            (
                r.get("key", ""),
                r.get("account", ""),
                r.get("scope", ""),
                r.get("name", ""),
                r.get("kind", ""),
            )
            for name in ("holdings", "instruments")
            for r in cleaned.get(name, [])
        },
        key=encoded,
    )
    result["holdings"] = [
        r for r in cleaned.get("holdings", []) if r.get("key") in keys
    ]
    result["instruments"] = [
        r for r in cleaned.get("instruments", []) if r.get("key") in keys
    ]
    f = cleaned.get("investment_followup") or {}
    scope = followup_scope(event.get("followup_scope"), context)
    choices = [
        r
        for r in f.get("choices", [])
        if not r.get("keys")
        or set(r.get("keys", [])) & keys
        or r.get("id") in scope["choice_ids"]
    ]
    result["investment_followup"] = dict(status=f.get("status"), choices=choices)
    etfs = cleaned.get("etf_context") or []
    rows = etfs if isinstance(etfs, list) else [etfs]
    result["etf_context"] = [
        r for r in rows if _etf_keys(r, portfolio_keys(context)) & keys
    ]
    return result


def event_context(event, context):
    """Model detail for an established event; retain the whole identity catalog.

    This is not a source filter. New evidence and unknown/broad dependencies use
    full context. ETF links are expanded before selecting detail, and all dates,
    weights and source qualifications of retained rows remain unchanged.
    """
    if review_dependencies(event.get("review_dependencies"), context)["all_portfolio"]:
        return deepcopy(context)
    keys = dependent_keys(event, context)
    result = deepcopy(context)
    rows = context.get("etf_context") or []
    if not isinstance(rows, list):
        return result  # Unknown future context shape must not silently lose data.
    result["etf_context"] = [
        deepcopy(r) for r in rows if _etf_keys(r, portfolio_keys(context)) & keys
    ]
    f = result.get("investment_followup")
    if isinstance(f, dict):
        scope = followup_scope(event.get("followup_scope"), context)
        choices = f.get("choices", [])
        f["choice_catalog"] = [
            {k: r[k] for k in ("id", "keys") if k in r} for r in choices
        ]
        f["choices"] = [
            r
            for r in choices
            if not r.get("keys")
            or set(r["keys"]) & keys
            or r.get("id") in scope["choice_ids"]
            or scope["all_choices"]
        ]
    if isinstance(result.get("exposures"), list):
        result["exposures"] = [r for r in result["exposures"] if r.get("key") in keys]
    result["detail_scope"] = dict(
        keys=sorted(keys),
        omitted_etfs=len(rows) - len(result["etf_context"]),
        full_context_available=True,
    )
    return result


def market_view(context, event):
    source = context.get("_review_market_observations")
    if not isinstance(source, dict):
        return None
    keys = dependent_keys(event or {}, context)
    return dict(
        status=source.get("status"),
        series=[deepcopy(r) for r in source.get("series", []) if r.get("key") in keys],
    )


def market_basis(context, event):
    view = market_view(context, event)
    if view is None:
        return None
    # Re-reading the same daily values is not a new market observation.
    return dict(
        status=view["status"],
        series=[
            {k: v for k, v in r.items() if k not in {"source_id", "available_at"}}
            for r in view["series"]
        ],
    )


def market_progress(previous, current):
    """Keep original observation and last assessment anchors through skipped ticks.

    A source's RETURN and POSITION units stay ratios; no percent unit guessing.
    These are observation anchors, never a made-up user risk acceptance level.
    """
    current_rows = {r["key"]: r for r in (current or {}).get("series", [])}
    result = []
    for label, view in [
        ("FIRST_OBSERVATION", (previous or {}).get("market_anchor")),
        (
            "LAST_ASSESSMENT",
            ((previous or {}).get("context_snapshot") or {}).get("market_observations"),
        ),
    ]:
        for old in (view or {}).get("series", []):
            new = current_rows.get(old["key"], {})
            if (
                old.get("status") != "DATED"
                or new.get("status") != "DATED"
                or old.get("formula") != new.get("formula")
                or old.get("adjustment_mode") != new.get("adjustment_mode")
                or new.get("session", "") <= old.get("session", "")
            ):
                continue
            changes = {}
            for name, point in old.get("metrics", {}).items():
                latest = new.get("metrics", {}).get(name)
                if latest and latest["unit"] == point["unit"]:
                    try:
                        delta = Decimal(latest["value"]) - Decimal(point["value"])
                        if delta.is_finite():
                            changes[name] = dict(
                                from_value=point["value"],
                                to_value=latest["value"],
                                delta=str(delta),
                                unit=point["unit"],
                            )
                    except InvalidOperation:
                        continue
            if changes:
                result.append(
                    dict(
                        anchor=label,
                        key=old["key"],
                        from_session=old["session"],
                        to_session=new["session"],
                        from_source=old["source_id"],
                        to_source=new["source_id"],
                        changes=changes,
                    )
                )
    return result


def first_market_anchor(previous, current):
    rows = {
        r["key"]: deepcopy(r)
        for r in (previous or {}).get("series", [])
        if r.get("status") == "DATED"
    }
    for row in (current or {}).get("series", []):
        if row.get("status") == "DATED":
            rows.setdefault(row["key"], deepcopy(row))
    return dict(status="OBSERVATION_ANCHOR", series=[rows[k] for k in sorted(rows)])
