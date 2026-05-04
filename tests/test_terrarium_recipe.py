import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


class TerrariumRecipeTests(unittest.TestCase):
    def test_terrarium_yaml_loads_with_installed_kohaku_schema(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_home:
            with patch.dict(os.environ, {"HOME": temp_home}, clear=False):
                from kohakuterrarium.terrarium.config import load_terrarium_config

                config = load_terrarium_config(repo_root / "terrarium.yaml")

        channels = {channel.name: channel.channel_type for channel in config.channels}
        creatures = {creature.name: creature for creature in config.creatures}

        self.assertEqual(
            channels,
            {
                "plan_ready": "queue",
                "web_client_plan_ready": "queue",
                "execution_done": "queue",
                "web_client_task": "queue",
                "web_client_done": "queue",
                "verification_request": "queue",
                "final_report": "broadcast",
                "human_review_queue": "queue",
            },
        )
        self.assertEqual(
            creatures["analysis_agent"].send_channels,
            ["plan_ready", "web_client_plan_ready"],
        )
        self.assertEqual(
            creatures["execution_agent"].listen_channels,
            ["plan_ready", "verification_request"],
        )
        self.assertEqual(creatures["execution_agent"].send_channels, ["execution_done"])
        self.assertEqual(
            creatures["web_client_agent"].listen_channels,
            ["web_client_plan_ready", "web_client_task"],
        )
        self.assertEqual(
            creatures["web_client_agent"].send_channels,
            ["execution_done", "web_client_done"],
        )
        self.assertEqual(creatures["report_agent"].listen_channels, ["execution_done"])
        self.assertEqual(
            creatures["report_agent"].send_channels,
            ["verification_request", "final_report", "human_review_queue"],
        )

    def test_creature_llm_selectors_resolve_to_provider_profiles(self):
        repo_root = Path(__file__).resolve().parents[1]
        creature_paths = [
            repo_root / "creatures" / "analysis",
            repo_root / "creatures" / "execution",
            repo_root / "creatures" / "web_client",
            repo_root / "creatures" / "report",
        ]

        with tempfile.TemporaryDirectory() as temp_home:
            with patch.dict(os.environ, {"HOME": temp_home}, clear=False):
                from kohakuterrarium.core.config import load_agent_config
                from kohakuterrarium.llm.profiles import resolve_controller_llm

                for creature_path in creature_paths:
                    config = load_agent_config(creature_path)
                    controller_data = {
                        "llm": config.llm_profile,
                        "model": config.model,
                        "provider": config.provider,
                        "temperature": config.temperature,
                    }
                    profile = resolve_controller_llm(controller_data)

                    self.assertIsNotNone(profile, creature_path.name)
                    self.assertEqual(profile.provider, "openrouter")
                    self.assertEqual(profile.api_key_env, "OPENROUTER_API_KEY")

    def test_creature_configs_use_send_message_not_channel_named_outputs(self):
        repo_root = Path(__file__).resolve().parents[1]
        for config_path in [
            repo_root / "creatures" / "analysis" / "config.yaml",
            repo_root / "creatures" / "execution" / "config.yaml",
            repo_root / "creatures" / "web_client" / "config.yaml",
            repo_root / "creatures" / "report" / "config.yaml",
        ]:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            tools = {tool["name"] for tool in config.get("tools", [])}
            self.assertIn("send_message", tools, config_path)

            output = config.get("output") or {}
            named_outputs = output.get("named_outputs") or {}
            channel_outputs = [
                name
                for name, item in named_outputs.items()
                if isinstance(item, dict) and item.get("type") == "channel"
            ]
            self.assertEqual(channel_outputs, [], config_path)

    def test_pure_web_recipe_can_omit_execution_agent(self):
        repo_root = Path(__file__).resolve().parents[1]
        recipe = {
            "version": "1.0",
            "creatures": [
                {
                    "name": "analysis_agent",
                    "base_config": str(repo_root / "creatures" / "analysis"),
                    "channels": {"can_send": ["web_client_plan_ready"]},
                },
                {
                    "name": "web_client_agent",
                    "base_config": str(repo_root / "creatures" / "web_client"),
                    "channels": {
                        "listen": ["web_client_plan_ready"],
                        "can_send": ["execution_done"],
                    },
                },
                {
                    "name": "report_agent",
                    "base_config": str(repo_root / "creatures" / "report"),
                    "channels": {
                        "listen": ["execution_done"],
                        "can_send": ["final_report", "human_review_queue"],
                    },
                },
            ],
            "channels": {
                "web_client_plan_ready": {"type": "queue"},
                "execution_done": {"type": "queue"},
                "final_report": {"type": "broadcast"},
                "human_review_queue": {"type": "queue"},
            },
            "root_agent": "analysis_agent",
        }

        with tempfile.TemporaryDirectory() as temp_home:
            recipe_path = Path(temp_home) / "pure_web.yaml"
            recipe_path.write_text(yaml.safe_dump(recipe), encoding="utf-8")
            with patch.dict(os.environ, {"HOME": temp_home}, clear=False):
                from kohakuterrarium.terrarium.config import load_terrarium_config

                config = load_terrarium_config(recipe_path)

        creature_names = {creature.name for creature in config.creatures}
        self.assertEqual(
            creature_names,
            {"analysis_agent", "web_client_agent", "report_agent"},
        )


if __name__ == "__main__":
    unittest.main()
