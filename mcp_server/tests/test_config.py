from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.config as mcp_config


def test_yaml_settings_expand_env_vars_and_apply_cli_overrides(tmp_path: Path) -> None:
    source = mcp_config.YamlSettingsSource(mcp_config.MCPServerConfig)

    with patch.dict("os.environ", {"API_KEY": "secret", "FLAG": "true"}, clear=True):
        expanded = source._expand_env_vars(
            {
                "token": "${API_KEY}",
                "enabled": "${FLAG:false}",
                "missing": "${NOT_SET}",
                "message": "Bearer ${API_KEY}",
            }
        )

    assert expanded["token"] == "secret"
    assert expanded["enabled"] is True
    assert expanded["missing"] is None
    assert expanded["message"] == "Bearer secret"
    assert source.get_field_value("server", None) is None

    namespace_config = mcp_config.NamespaceConfig(episode_id_prefix=None)
    assert namespace_config.episode_id_prefix == ""

    server_config = mcp_config.MCPServerConfig()
    server_config.apply_cli_overrides(
        SimpleNamespace(
            transport="stdio",
            llm_provider="bedrock",
            model="sonnet",
            temperature=0.2,
            embedder_provider="bedrock",
            embedder_model="titan",
            database_provider="neptune",
            group_id="course-team",
            user_id="student1",
        )
    )

    assert server_config.server.transport == "stdio"
    assert server_config.llm.provider == "bedrock"
    assert server_config.llm.model == "sonnet"
    assert server_config.embedder.provider == "bedrock"
    assert server_config.database.provider == "neptune"
    assert server_config.namespace.group_id == "course-team"
    assert server_config.namespace.user_id == "student1"

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n"
        "  host: ${HOST:127.0.0.1}\n"
        "  port: 9000\n"
        "namespace:\n"
        "  episode_id_prefix: ${PREFIX:cg}\n",
        encoding="utf-8",
    )

    yaml_source = mcp_config.YamlSettingsSource(mcp_config.MCPServerConfig, config_path)
    with patch.dict("os.environ", {"HOST": "0.0.0.0"}, clear=True):
        loaded = yaml_source()

    assert loaded["server"]["host"] == "0.0.0.0"
    assert loaded["server"]["port"] == 9000
    assert loaded["namespace"]["episode_id_prefix"] == "cg"
