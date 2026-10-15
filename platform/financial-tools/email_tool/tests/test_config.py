import pytest

from email_tool.config import ConfigError, SmtpConfig, env_var_name, load_smtp_config


def make_env(**overrides):
    env = {
        "EMAILTOOL_ACME_SMTP_HOST": "smtp.acme.example.com",
        "EMAILTOOL_ACME_FROM": "billing@acme.example.com",
    }
    env.update(overrides)
    return env


def test_env_var_name_uppercases_the_tenant_and_appends_the_setting():
    assert env_var_name("acme", "SMTP_HOST") == "EMAILTOOL_ACME_SMTP_HOST"


def test_loads_a_complete_tenant_configuration():
    config = load_smtp_config(
        "acme",
        make_env(
            EMAILTOOL_ACME_SMTP_USER="billing@acme.example.com",
            EMAILTOOL_ACME_SMTP_PASSWORD="hunter2",
            EMAILTOOL_ACME_SMTP_TLS="ssl",
            EMAILTOOL_ACME_SMTP_PORT="2525",
        ),
    )
    assert config == SmtpConfig(
        host="smtp.acme.example.com",
        port=2525,
        from_address="billing@acme.example.com",
        username="billing@acme.example.com",
        password="hunter2",
        tls="ssl",
    )


def test_defaults_to_starttls_on_its_standard_port_without_credentials():
    config = load_smtp_config("acme", make_env())
    assert config.tls == "starttls"
    assert config.port == 587
    assert config.username is None
    assert config.password is None


@pytest.mark.parametrize(
    ("tls", "expected_port"),
    [("starttls", 587), ("ssl", 465), ("none", 25)],
)
def test_the_default_port_follows_the_tls_mode(tls, expected_port):
    config = load_smtp_config("acme", make_env(EMAILTOOL_ACME_SMTP_TLS=tls))
    assert config.tls == tls
    assert config.port == expected_port


def test_tls_mode_is_case_insensitive():
    config = load_smtp_config("acme", make_env(EMAILTOOL_ACME_SMTP_TLS="SSL"))
    assert config.tls == "ssl"


def test_missing_host_names_the_environment_variable():
    env = make_env()
    del env["EMAILTOOL_ACME_SMTP_HOST"]
    with pytest.raises(ConfigError, match="EMAILTOOL_ACME_SMTP_HOST"):
        load_smtp_config("acme", env)


def test_missing_from_address_names_the_environment_variable():
    env = make_env()
    del env["EMAILTOOL_ACME_FROM"]
    with pytest.raises(ConfigError, match="EMAILTOOL_ACME_FROM"):
        load_smtp_config("acme", env)


def test_missing_settings_are_listed_together():
    with pytest.raises(ConfigError) as excinfo:
        load_smtp_config("acme", {})
    message = str(excinfo.value)
    assert "EMAILTOOL_ACME_FROM" in message
    assert "EMAILTOOL_ACME_SMTP_HOST" in message


def test_blank_values_are_treated_as_missing():
    with pytest.raises(ConfigError, match="EMAILTOOL_ACME_SMTP_HOST"):
        load_smtp_config("acme", make_env(EMAILTOOL_ACME_SMTP_HOST=""))


@pytest.mark.parametrize(
    "setting",
    ["EMAILTOOL_ACME_SMTP_USER", "EMAILTOOL_ACME_SMTP_PASSWORD"],
)
def test_credentials_must_be_set_together(setting):
    env = make_env(
        EMAILTOOL_ACME_SMTP_USER="billing@acme.example.com",
        EMAILTOOL_ACME_SMTP_PASSWORD="hunter2",
    )
    del env[setting]
    with pytest.raises(ConfigError, match="must be set together"):
        load_smtp_config("acme", env)


def test_unknown_tls_mode_is_rejected():
    with pytest.raises(ConfigError, match="invalid TLS mode"):
        load_smtp_config(
            "acme", make_env(EMAILTOOL_ACME_SMTP_TLS="carrier-pigeon")
        )


def test_explicit_port_is_used():
    config = load_smtp_config("acme", make_env(EMAILTOOL_ACME_SMTP_PORT="2525"))
    assert config.port == 2525


def test_non_integer_port_is_rejected():
    with pytest.raises(ConfigError, match="must be an integer"):
        load_smtp_config(
            "acme", make_env(EMAILTOOL_ACME_SMTP_PORT="not-a-port")
        )


@pytest.mark.parametrize("port", ["0", "65536"])
def test_out_of_range_port_is_rejected(port):
    with pytest.raises(ConfigError, match="outside 1-65535"):
        load_smtp_config("acme", make_env(EMAILTOOL_ACME_SMTP_PORT=port))


@pytest.mark.parametrize(
    "tenant_id", ["Acme", "-acme", "acme!", "", "a" * 65]
)
def test_invalid_tenant_ids_are_rejected(tenant_id):
    with pytest.raises(ConfigError, match="invalid tenant id"):
        load_smtp_config(tenant_id, make_env())


@pytest.mark.parametrize("tenant_id", ["a", "t-1", "t_2", "a" * 64])
def test_valid_tenant_ids_are_accepted(tenant_id):
    env = {
        env_var_name(tenant_id, "SMTP_HOST"): "smtp.example.com",
        env_var_name(tenant_id, "FROM"): "billing@example.com",
    }
    config = load_smtp_config(tenant_id, env)
    assert config.host == "smtp.example.com"


def test_password_is_excluded_from_repr():
    config = load_smtp_config(
        "acme",
        make_env(
            EMAILTOOL_ACME_SMTP_USER="billing@acme.example.com",
            EMAILTOOL_ACME_SMTP_PASSWORD="hunter2",
        ),
    )
    assert "hunter2" not in repr(config)


def test_a_passed_environ_is_used_instead_of_the_process_environment(monkeypatch):
    monkeypatch.setenv("EMAILTOOL_ACME_SMTP_HOST", "smtp.process.example.com")
    monkeypatch.setenv("EMAILTOOL_ACME_FROM", "billing@process.example.com")
    with pytest.raises(ConfigError, match="missing environment variables"):
        load_smtp_config("acme", {})
