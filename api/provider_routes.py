"""Provider routing with composite model IDs.

Each model is uniquely identified by a composite ID "Provider::Model",
allowing the same model name to exist across multiple providers without
the frontend collapsing them or the backend's provider map being overwritten.

Backward compatibility: when a bare model name is passed (e.g. from CC sessions
or legacy session data), route_provider falls back to the first matching provider.
"""
import json
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROVIDERS_PATH = os.path.join(_BASE_DIR, "providers.json")
_USER_MODELS_PATH = os.path.join(_BASE_DIR, "user_models.json")

SEPARATOR = "::"

# Runtime caches — keyed by composite_id
_MODEL_MAP = {}           # composite_id -> {api_url, api_key, clean_name}
_ALL_MODELS = []          # ordered list of composite IDs
_PRICE_MAP = {}           # composite_id -> price_per_call
_SEGMENTED_MAP = {}       # composite_id -> bool (whether to use cache segmentation)
_SUBAGENT_PROVIDERS = []  # [{name, api_key, url, model}]
_MODEL_PROVIDER_MAP = {}  # composite_id -> provider_name
_WEB_SEARCH_CONFIG = {}   # {provider, api_key} from providers.json's web_search block


def make_composite_id(provider_name, model_name):
    """Construct a composite ID from provider name and model name."""
    return f"{provider_name}{SEPARATOR}{model_name}"


def parse_composite_id(composite_id):
    """Return (provider_name_or_None, model_name) tuple.

    If the input is not a composite ID, provider_name is None and
    model_name is the input itself.
    """
    if composite_id and SEPARATOR in composite_id:
        idx = composite_id.index(SEPARATOR)
        return composite_id[:idx], composite_id[idx + len(SEPARATOR):]
    return None, composite_id or ""


def strip_composite(name):
    """Extract the bare model name from a composite ID, or pass through.

    Safe to call on any string — legacy bare names are returned unchanged.
    Used by code paths that check prefixes like [ARC3] or [本地] in model names.
    """
    _, mn = parse_composite_id(name or "")
    return mn


def _load_providers():
    """Load providers.json and user_models.json, build routing tables."""
    global _MODEL_MAP, _ALL_MODELS, _PRICE_MAP, _SEGMENTED_MAP, _SUBAGENT_PROVIDERS, _MODEL_PROVIDER_MAP, _WEB_SEARCH_CONFIG
    _MODEL_MAP = {}
    _ALL_MODELS = []
    _PRICE_MAP = {}
    _SEGMENTED_MAP = {}
    _SUBAGENT_PROVIDERS = []
    _MODEL_PROVIDER_MAP = {}
    _WEB_SEARCH_CONFIG = {}

    paths = [p for p in [_PROVIDERS_PATH, _USER_MODELS_PATH] if os.path.exists(p)]
    if not paths:
        return

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"Failed to load {os.path.basename(path)}: {e}")
            continue

        if isinstance(raw, dict):
            providers = raw.get("model_providers", raw.get("providers", []))
            search_cfg = raw.get("web_search", {})
            if search_cfg:
                _WEB_SEARCH_CONFIG = search_cfg
        else:
            providers = raw

        for provider in providers:
            api_key = provider.get("api_key", "")
            api_url = provider.get("api_url", "")
            models = provider.get("models", [])
            prov_name = provider.get("name", "Unknown")

            for model in models:
                if isinstance(model, dict):
                    model_name = model["name"]
                    price = model.get("price_per_call", 0)
                    segmented = model.get("segmented", False)
                else:
                    model_name = model
                    price = 0
                    segmented = False

                cid = make_composite_id(prov_name, model_name)
                _MODEL_PROVIDER_MAP[cid] = prov_name
                _ALL_MODELS.append(cid)
                if price > 0:
                    _PRICE_MAP[cid] = price
                _SEGMENTED_MAP[cid] = segmented
                _MODEL_MAP[cid] = {
                    "api_url": api_url,
                    "api_key": api_key,
                    "clean_name": model_name,
                }

            sa_cfg = provider.get("subagent")
            if sa_cfg:
                sa_key = sa_cfg.get("api_key", "") or api_key
                sa_url = sa_cfg.get("url", "")
                sa_models = sa_cfg.get("models", [])
                if not sa_models:
                    _single = sa_cfg.get("model", "")
                    sa_models = [_single] if _single else [""]
                for _sa_model in sa_models:
                    _display = f"{prov_name} - {_sa_model}" if _sa_model else prov_name
                    _SUBAGENT_PROVIDERS.append({
                        "name": _display,
                        "api_key": sa_key,
                        "url": sa_url,
                        "model": _sa_model,
                    })


def route_provider(model_id_or_name, config):
    """Route a model identifier to (api_url, api_key, clean_model_name).

    Accepts either a composite ID "Provider::Model" (exact match) or a
    bare model name (legacy; resolves to the first matching provider).
    """
    if model_id_or_name in _MODEL_MAP:
        entry = _MODEL_MAP[model_id_or_name]
        return entry["api_url"], entry["api_key"], entry["clean_name"]

    if model_id_or_name:
        for cid, entry in _MODEL_MAP.items():
            _, mn = parse_composite_id(cid)
            if mn == model_id_or_name:
                return entry["api_url"], entry["api_key"], entry["clean_name"]

    return config.get("API_URL", ""), config.get("API_KEY", ""), model_id_or_name


def get_all_models():
    """Return the full ordered list of composite model IDs."""
    return list(_ALL_MODELS)


def get_model_price(model_id):
    """Return the per-call price for a model ID (supports composite or bare)."""
    if model_id in _PRICE_MAP:
        return _PRICE_MAP[model_id]
    for cid, price in _PRICE_MAP.items():
        _, mn = parse_composite_id(cid)
        if mn == model_id:
            return price
    return 0


def is_model_segmented(model_id):
    """Check if a model has cache segmentation enabled. Default True unless explicitly disabled."""
    if model_id in _SEGMENTED_MAP:
        return _SEGMENTED_MAP[model_id]
    for cid, val in _SEGMENTED_MAP.items():
        _, mn = parse_composite_id(cid)
        if mn == model_id:
            return val
    return True


def get_subagent_providers():
    """Return the list of subagent provider configs."""
    return list(_SUBAGENT_PROVIDERS)


def get_web_search_config():
    """Return the web_search provider configuration from providers.json."""
    return dict(_WEB_SEARCH_CONFIG)


def get_model_providers():
    """Return mapping from composite_id to provider_name."""
    return dict(_MODEL_PROVIDER_MAP)


def reload_providers():
    """Reload JSON config files at runtime."""
    _load_providers()


# Load on import
_load_providers()
