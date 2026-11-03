"""The API key check (docs/decisions.md #31): /ask and /tickets need it, /health does not, and
the app refuses to start without a usable key."""

import logging

import pytest
from fastapi.testclient import TestClient

from fakes import API_KEY
from support_desk_assistant.api import __main__ as api_main
from support_desk_assistant.api.app import create_app
from support_desk_assistant.api.auth import AuthConfigError, check_auth_config
from support_desk_assistant.config import ApiSettings, Settings
from support_desk_assistant.triage.store import TicketStore
from test_api_tickets import Stub, ok

PROTECTED = [
    ("post", "/ask", {"question": "How do I apply?"}),
    ("post", "/tickets", {"body": "My EI payment stopped"}),
    ("get", "/tickets", None),
    ("get", "/tickets/1", None),
    ("post", "/tickets/1/override", {}),
]


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    # A developer's own SDA_API_KEY must not change what these tests see.
    monkeypatch.delenv("SDA_API_KEY", raising=False)


def build(tmp_path, **settings_kw):
    asked = []
    app = create_app(
        Settings(_env_file=None, **settings_kw),
        ask_fn=lambda *a: asked.append(a),
        triage_fn=Stub(ok()),
        store=TicketStore(tmp_path / "t.sqlite"),
    )
    return TestClient(app, raise_server_exceptions=False), asked


def send(client, method, path, body, headers=None):
    kwargs = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_no_key_is_refused(tmp_path, method, path, body):
    client, asked = build(tmp_path, sda_api_key=API_KEY)
    r = send(client, method, path, body)
    assert r.status_code == 401
    assert r.json() == {"detail": "missing or invalid API key"}
    assert r.headers["www-authenticate"] == "ApiKey"
    assert asked == []


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_a_wrong_key_gets_the_same_answer_as_no_key(tmp_path, method, path, body):
    client, _ = build(tmp_path, sda_api_key=API_KEY)
    wrong = send(client, method, path, body, {"X-API-Key": API_KEY + "x"})
    missing = send(client, method, path, body)
    assert wrong.status_code == 401 and wrong.json() == missing.json()


def test_a_key_that_is_not_ascii_is_refused_not_a_crash(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY)
    r = client.get("/tickets", headers={"X-API-Key": b"caf\xe9"})
    assert r.status_code == 401


def test_the_right_key_gets_through(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY)
    headers = {"X-API-Key": API_KEY}
    assert (
        client.post("/tickets", json={"body": "My EI payment stopped"}, headers=headers).status_code
        == 201
    )
    assert client.get("/tickets", headers=headers).status_code == 200
    assert client.get("/tickets/1", headers=headers).status_code == 200
    assert client.post("/tickets/1/override", json={}, headers=headers).status_code == 200


def test_a_refused_ticket_is_not_stored(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY)
    client.post("/tickets", json={"body": "My EI payment stopped"})
    assert client.get("/tickets", headers={"X-API-Key": API_KEY}).json() == []


def test_health_needs_no_key(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY)
    assert client.get("/health").json() == {"status": "ok"}


def test_the_header_name_is_configurable(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY, api=ApiSettings(key_header="X-Desk-Key"))
    assert client.get("/tickets", headers={"X-API-Key": API_KEY}).status_code == 401
    assert client.get("/tickets", headers={"X-Desk-Key": API_KEY}).status_code == 200


def test_the_app_will_not_start_without_a_key(tmp_path):
    with pytest.raises(AuthConfigError, match="SDA_API_KEY is not set"):
        build(tmp_path)


def test_an_empty_key_counts_as_no_key(tmp_path):
    # `.env.example` ships `SDA_API_KEY=`; copying it unchanged must not switch the check off.
    with pytest.raises(AuthConfigError, match="not set"):
        build(tmp_path, sda_api_key="")


def test_a_short_key_is_refused(tmp_path):
    with pytest.raises(AuthConfigError, match="at least 24 characters"):
        build(tmp_path, sda_api_key="short")


def test_no_key_is_allowed_on_loopback_when_asked_for(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        client, asked = build(tmp_path, api=ApiSettings(allow_no_key=True))
    assert "key check is off" in caplog.text
    assert client.get("/tickets").status_code == 200
    assert client.post("/ask", json={"question": "q"}).status_code != 401
    assert len(asked) == 1


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.test"])
def test_no_key_is_never_allowed_off_loopback(tmp_path, host):
    with pytest.raises(AuthConfigError, match="loopback"):
        build(tmp_path, api=ApiSettings(allow_no_key=True, host=host))


def test_a_key_works_off_loopback(tmp_path):
    client, _ = build(tmp_path, sda_api_key=API_KEY, api=ApiSettings(host="0.0.0.0"))
    assert client.get("/tickets").status_code == 401
    assert client.get("/tickets", headers={"X-API-Key": API_KEY}).status_code == 200


def test_the_key_comes_from_sda_api_key_in_the_environment(monkeypatch):
    monkeypatch.setenv("SDA_API_KEY", API_KEY)
    settings = Settings(_env_file=None)
    assert settings.api_key.get_secret_value() == API_KEY
    check_auth_config(settings)


def test_a_generic_api_key_variable_is_not_picked_up(monkeypatch):
    monkeypatch.delenv("SDA_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "x" * 40)
    assert Settings(_env_file=None).api_key is None


def test_allow_no_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SDA_API__ALLOW_NO_KEY", "true")
    assert Settings(_env_file=None).api.allow_no_key is True


def test_the_key_is_not_shown_when_settings_are_printed():
    assert API_KEY not in repr(Settings(sda_api_key=API_KEY))


def test_sda_api_exits_with_a_message_instead_of_starting(monkeypatch):
    monkeypatch.setattr(api_main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        api_main.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start")
    )
    with pytest.raises(SystemExit) as exc:
        api_main.main()
    assert "SDA_API_KEY is not set" in str(exc.value)


def test_sda_api_starts_with_a_key(monkeypatch):
    started = {}
    monkeypatch.setattr(api_main, "get_settings", lambda: Settings(sda_api_key=API_KEY))
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **k: started.update(k))
    api_main.main()
    assert started["host"] == "127.0.0.1" and started["port"] == 8000
