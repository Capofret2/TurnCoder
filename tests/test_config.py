"""config.CONFIG shape and the _env / _env_json helpers.

This loader fails silently by design: a missing key becomes an empty string and
nothing raises, which is why the documented symptom of skipping the
providers.example.json copy is "the service starts and the model dropdown is
simply empty". The assertions here are structural on purpose — asserting actual
key values would make the suite a function of whatever .env the machine happens
to carry.
"""
import pytest

import config

REQUIRED_KEYS = [
    'API_KEY', 'MODEL_NAME', 'SYSTEM_PROMPT', 'THINKING_CONFIG', 'API_URL',
    'LOCAL_API_URL', 'SUBAGENT_PROVIDERS',
]


def test_config_exposes_every_key_the_app_reads():
    for key in REQUIRED_KEYS:
        assert key in config.CONFIG, 'CONFIG lost the %s key' % key


def test_thinking_config_shape():
    tc = config.CONFIG['THINKING_CONFIG']
    assert isinstance(tc, dict)
    assert tc['thinking_level'] == 'high'
    assert tc['include_thoughts'] is True


def test_subagent_providers_is_always_a_list():
    """provider_routes iterates it, so a non-list would break at time."""
    assert isinstance(config.CONFIG['SUBAGENT_PROVIDERS'], list)


def test_every_url_key_is_a_string():
    for key, value in config.CONFIG.items():
        if key.endswith('_URL'):
            assert isinstance(value, str), '%s is not a string' % key


# ------------------------------------------------------------------ _env

def test_env_returns_the_default_when_unset(monkeypatch):
    monkeypatch.delenv('TURNCODER_TEST_ONLY', raising=False)
    assert config._env('TURNCODER_TEST_ONLY', 'fallback') == 'fallback'


def test_env_default_default_is_empty_string(monkeypatch):
    monkeypatch.delenv('TURNCODER_TEST_ONLY', raising=False)
    assert config._env('TURNCODER_TEST_ONLY') == ''


def test_env_prefers_the_environment(monkeypatch):
    monkeypatch.setenv('TURNCODER_TEST_ONLY', 'from-env')
    assert config._env('TURNCODER_TEST_ONLY', 'fallback') == 'from-env'


def test_env_treats_an_empty_variable_as_a_value(monkeypatch):
    """Characterisation, not endorsement.

    os.getenv only falls back when the name is absent, so `MODEL_NAME=` in .env
    yields '' instead of the built-in default — and an empty model name is one
    route to the silent "starts up with no models" state. Switching to an `or`
    fallback would be a behaviour change; this test exists so that change has to
    be deliberate rather than incidental.
    """
    monkeypatch.setenv('TURNCODER_TEST_ONLY', '')
    assert config._env('TURNCODER_TEST_ONLY', 'fallback') == ''


# -------------------------------------------------------------- _env_json

def test_env_json_parses_a_json_array(monkeypatch):
    monkeypatch.setenv('TURNCODER_TEST_JSON', '[{"name": "p"}]')
    assert config._env_json('TURNCODER_TEST_JSON') == [{'name': 'p'}]


def test_env_json_falls_back_on_malformed_json(monkeypatch):
    """Malformed config degrades to the default rather than crashing import."""
    monkeypatch.setenv('TURNCODER_TEST_JSON', '{not json')
    assert config._env_json('TURNCODER_TEST_JSON', ['d']) == ['d']


def test_env_json_falls_back_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv('TURNCODER_TEST_JSON', raising=False)
    assert config._env_json('TURNCODER_TEST_JSON', ['d']) == ['d']
    monkeypatch.setenv('TURNCODER_TEST_JSON', '')
    assert config._env_json('TURNCODER_TEST_JSON', ['d']) == ['d']


def test_env_json_default_default_is_empty_list(monkeypatch):
    monkeypatch.delenv('TURNCODER_TEST_JSON', raising=False)
    assert config._env_json('TURNCODER_TEST_JSON') == []
    monkeypatch.setenv('TURNCODER_TEST_JSON', '{not json')
    assert config._env_json('TURNCODER_TEST_JSON') == []