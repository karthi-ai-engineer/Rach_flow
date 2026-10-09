"""What meet_answer reads from Rflow: the current profile's settings (speech model, AI model, vocabulary) and its keys.

Read again for every question, so a model or key changed in Rflow is used at once; never written. The keys stay in
GatewayConfig, whose repr hides them, and never reach a log.
"""
from dataclasses import dataclass

from sst.gateway import GatewayConfig
from sst.settings import Profiles, Settings


@dataclass(frozen=True)
class RflowSetup:
    settings: Settings
    gateway: GatewayConfig

    @classmethod
    def load(cls) -> "RflowSetup":
        profile = Profiles.load().current
        return cls(Settings.load(profile.settings_file), GatewayConfig.load(profile.gateway_file))

    def ai_model(self, override: str = "") -> str:
        """meet_answer's own choice, else the model chosen for Rflow's AI cleanup; "" while no provider is chosen."""
        return (override or self.settings.cleanup_model).strip() if self.gateway.chosen else ""
