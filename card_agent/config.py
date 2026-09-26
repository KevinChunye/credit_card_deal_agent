"""Configuration from environment variables only (no secrets in code or files).

See .env.example for the full list. Nothing here loads a .env file: on Maritime
the variables come from the agent's settings; locally, export them yourself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_STATE_DIR = Path("~/.credit_card_deal_agent").expanduser()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    data_repo: str = "KevinChunye/credit_card_deal_agent"
    data_branch: str = "data"
    github_token: str | None = None
    agentmail_api_key: str | None = None
    agentmail_inbox: str | None = None
    owner_email: str | None = None
    digest_to_email: str | None = None

    @property
    def state_dir(self) -> Path:
        return self.db_path.parent

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def last_response_path(self) -> Path:
        return self.state_dir / "last_response.json"

    @property
    def allowed_recipients(self) -> set[str]:
        """The only addresses the agent may ever email."""
        addresses = {self.owner_email, self.digest_to_email}
        return {a.strip().lower() for a in addresses if a and a.strip()}

    @property
    def digest_recipient(self) -> str | None:
        return self.digest_to_email or self.owner_email

    @classmethod
    def from_env(cls) -> Settings:
        db = os.environ.get("CARD_AGENT_DB") or str(DEFAULT_STATE_DIR / "state.db")
        return cls(
            db_path=Path(db).expanduser(),
            data_repo=os.environ.get("CARD_AGENT_DATA_REPO") or cls.data_repo,
            data_branch=os.environ.get("CARD_AGENT_DATA_BRANCH") or cls.data_branch,
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            agentmail_api_key=os.environ.get("AGENTMAIL_API_KEY") or None,
            agentmail_inbox=os.environ.get("AGENTMAIL_INBOX") or None,
            owner_email=os.environ.get("OWNER_EMAIL") or None,
            digest_to_email=os.environ.get("DIGEST_TO_EMAIL") or None,
        )
