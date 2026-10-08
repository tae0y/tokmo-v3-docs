"""Model registry: one place (var/data/models.json) that names which model answers each stage.

Pure values: parsing, validation and the known-model catalog. No transport, no file writes.

A *spec* is a string ``[provider:]model[@effort]``:

* ``glm-5.3``                      Z.AI direct HTTP (provider ``zai`` is the default when no prefix)
* ``codex:gpt-5.6-terra@low``      Codex CLI (ChatGPT subscription login), reasoning effort ``low``
* ``claude:claude-haiku-4-5-20251001``  Claude CLI (Claude subscription login)

A stage entry is one spec or an ordered list of specs (a *chain*). The first member is the
preferred model; the runtime moves to the next member only when the current one fails with a
QUOTA, AUTH or TRANSIENT category, and comes back automatically (see programs/model_chain.py).
A chain never falls back because of a bad answer, and never to a provider that is not listed.

Model ids are checked against a catalog so a typo (``gpt-5-6-terra``) is rejected at load
time instead of failing on the first live call. The Codex catalog is read from the CLI's own
``~/.codex/models_cache.json`` when present, so the list is what that subscription can use.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

REGISTRY_SCHEMA = 'tokmo-models-v1'
REGISTRY_FILE = 'models.json'
PROVIDERS = ('zai', 'codex', 'claude')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'default')
STAGES = {'news': ('screen', 'first_pass', 'confirm'),
          'paper': ('decide',),            # opportunity-rotation PAPER decision (one model, no chain)
          'quant': ('rotation_entry',)}    # hyundai rotation -> existing Quant analysis entry (one model, no chain)
# Stages that take exactly one model: the product gate pins one exact id per call.
SINGLE_MODEL_STAGES = {('paper', 'decide'), ('quant', 'rotation_entry')}
DEFAULT_SINGLE_MODELS = {('paper', 'decide'): 'codex:gpt-6-astra', ('quant', 'rotation_entry'): 'codex:gpt-6-astra'}
# Known ids per provider. Codex is extended/replaced by the CLI's cached catalog at check time.
KNOWN_MODELS = {
    'zai': ('glm-5.3', 'glm-5.3-flash', 'glm-5.3-air', 'glm-5.2'),
    'codex': ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5'),
    'claude': ('claude-fable-5-1', 'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001'),
}
_ID = re.compile(r'[a-z0-9][a-z0-9.-]{0,127}')


class ModelRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    effort: str | None = None      # None = the stage's default effort
    transport: str | None = None   # None = the stage's default; 'direct' (CLI/HTTP from this process) or 'hermes' (shared launcher)

    @property
    def name(self):
        """The id the rest of the code base uses: ``glm-5.3`` or ``codex:gpt-5.6-terra``."""
        return self.model if self.provider == 'zai' else self.provider + ':' + self.model

    def __str__(self):
        return self.name + ('@' + self.effort if self.effort else '')


def parse_spec(value):
    """``"codex:gpt-5.6-terra@low"`` or ``{"model": ..., "effort": ..., "transport": ...}`` -> ModelSpec."""
    if isinstance(value, ModelSpec):
        return value
    transport = None
    effort = None
    if isinstance(value, dict):
        extra = set(value) - {'model', 'effort', 'transport'}
        if extra or not isinstance(value.get('model'), str):
            raise ModelRegistryError('model entry must be {"model": "...", "effort"?: "...", "transport"?: "..."}')
        transport = value.get('transport')
        if transport not in (None, 'direct', 'hermes'):
            raise ModelRegistryError('transport must be direct or hermes: ' + str(transport))
        effort = value.get('effort')
        value = value['model']
    if not isinstance(value, str) or not value.strip():
        raise ModelRegistryError('model spec must be a non-empty string')
    text = value.strip()
    if '@' in text:
        text, suffix = text.rsplit('@', 1)
        if effort is not None and effort != suffix:
            raise ModelRegistryError('effort given twice: ' + value)
        effort = suffix
    if effort is not None and effort not in EFFORTS:
        raise ModelRegistryError('unknown reasoning effort: ' + str(effort))
    provider, _, model = text.rpartition(':')
    provider = provider or 'zai'
    if provider not in PROVIDERS:
        raise ModelRegistryError('unknown provider "%s" (use %s)' % (provider, ', '.join(PROVIDERS)))
    if not _ID.fullmatch(model) or model == 'latest':
        raise ModelRegistryError('non-canonical model id: ' + model)
    if transport == 'hermes' and provider != 'codex':
        raise ModelRegistryError('transport hermes is only available for codex models')
    if effort is not None and provider == 'zai':
        raise ModelRegistryError('@effort applies to codex/claude models only: ' + value)
    if effort == 'default' and provider == 'codex':
        raise ModelRegistryError('codex effort must be low/medium/high/xhigh/max: ' + value)
    return ModelSpec(provider, model, effort, transport)


def parse_chain(value):
    """One spec or a list of specs -> tuple of ModelSpec (never empty, no duplicate ids)."""
    items = value if isinstance(value, list) else [value]
    if not items:
        raise ModelRegistryError('a stage needs at least one model')
    chain = tuple(parse_spec(item) for item in items)
    names = [spec.name for spec in chain]
    if len(set(names)) != len(names):
        raise ModelRegistryError('a chain lists each model once: ' + ', '.join(names))
    return chain


def codex_catalog(codex_home):
    """Model ids the installed Codex CLI can use, from its own cache.

    None when the cache is absent or unreadable (the built-in list applies); a tuple, possibly
    empty, when it was read. An empty visible list is a real answer: nothing is usable."""
    path = Path(codex_home) / 'models_cache.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        models = [row['slug'] for row in payload['models'] if row.get('visibility') != 'hide']
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return tuple(models)


def known_models(codex_home=None):
    catalog = dict(KNOWN_MODELS)
    live = codex_catalog(codex_home) if codex_home is not None else None
    if live is not None:
        catalog['codex'] = live
    return catalog


def unknown_models(chain, catalog):
    """Specs whose id is not in the catalog for their provider (an empty tuple means all known)."""
    return tuple(spec for spec in chain if spec.model not in catalog.get(spec.provider, ()))


def check_known(chains, catalog, *, module='news'):
    """Reject an effective configuration that names a model id outside the catalog, before any call.

    ``chains`` maps stage -> chain (or None). Raises ModelRegistryError naming every unknown id."""
    problems = []
    for stage, chain in chains.items():
        for spec in unknown_models(chain or (), catalog):
            problems.append('%s.%s: %s (known %s ids: %s)' % (module, stage, spec.name, spec.provider,
                                                                ', '.join(catalog.get(spec.provider, ())) or '-'))
    if problems:
        raise ModelRegistryError('unknown model id: ' + '; '.join(problems))


def load_registry(path):
    """Read models.json. Missing file -> {} (every stage keeps its module config). Invalid -> error."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ModelRegistryError('models.json unreadable: ' + type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get('schema') != REGISTRY_SCHEMA:
        raise ModelRegistryError('models.json needs "schema": "%s"' % REGISTRY_SCHEMA)
    registry = {}
    for module, stages in payload.items():
        if module == 'schema' or module.startswith('_'):   # "_comment" and friends are ignored
            continue
        if module not in STAGES:
            raise ModelRegistryError('unknown module in models.json: ' + str(module))
        if not isinstance(stages, dict):
            raise ModelRegistryError('module %s must map stage -> model(s)' % module)
        registry[module] = {}
        for stage, value in stages.items():
            if stage not in STAGES[module]:
                raise ModelRegistryError('unknown stage %s.%s (stages: %s)' % (module, stage, ', '.join(STAGES[module])))
            chain = parse_chain(value)
            if (module, stage) in SINGLE_MODEL_STAGES and len(chain) != 1:
                raise ModelRegistryError('%s.%s takes exactly one model (no chain)' % (module, stage))
            registry[module][stage] = chain
    return registry


def single_model(registry, module, stage):
    """The one ModelSpec for a single-model stage: models.json first, else the built-in default."""
    if (module, stage) not in SINGLE_MODEL_STAGES:
        raise ModelRegistryError('not a single-model stage: %s.%s' % (module, stage))
    chain = registry.get(module, {}).get(stage)
    return chain[0] if chain else parse_spec(DEFAULT_SINGLE_MODELS[(module, stage)])


def registry_path(project_root):
    return Path(project_root) / 'var' / 'data' / REGISTRY_FILE


def news_chains(registry, config):
    """Effective NEWS chains: models.json first, else the legacy keys in news_decision.json.

    ``confirm`` is the primary analysis model (``analysis_model``); ``first_pass`` is optional
    (None keeps the single-model analysis path). ``screen`` always exists."""
    stages = registry.get('news', {})
    def legacy(key, default=None):
        value = config.get(key, default)
        return parse_chain(value) if value else None
    screen = stages.get('screen') or legacy('screen_model', 'glm-5.3')
    confirm = stages.get('confirm') or legacy('analysis_model', 'glm-5.3')
    first_pass = stages.get('first_pass') or legacy('first_pass_model')
    return dict(screen=screen, first_pass=first_pass, confirm=confirm)
