import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
                "execution_done": "queue",
                "verification_request": "queue",
                "final_report": "broadcast",
                "human_review_queue": "queue",
            },
        )
        self.assertEqual(creatures["analysis_agent"].send_channels, ["plan_ready"])
        self.assertEqual(
            creatures["execution_agent"].listen_channels,
            ["plan_ready", "verification_request"],
        )
        self.assertEqual(creatures["execution_agent"].send_channels, ["execution_done"])
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


if __name__ == "__main__":
    unittest.main()
