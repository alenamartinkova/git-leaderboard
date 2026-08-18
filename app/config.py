import os
import re
from dataclasses import dataclass

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

# Tokeny sú per-org premenné s dynamickým názvom (GITHUB_TOKEN_<ORG>), takže sa
# nedajú deklarovať ako polia. Čítame ich priamo — v dockeri sú v prostredí,
# lokálne v .env súbore.
_DOTENV: dict[str, str | None] = dotenv_values(".env")


def _env(key: str) -> str:
    return (os.environ.get(key) or _DOTENV.get(key) or "").strip()


def token_env_key(org: str) -> str:
    """EsportDynamics -> GITHUB_TOKEN_ESPORTDYNAMICS"""
    return "GITHUB_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", org).upper()


@dataclass(frozen=True)
class OrgConfig:
    name: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.name and self.token)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Jeden org (staré nastavenie) alebo viac cez GITHUB_ORGS.
    github_org: str = ""
    github_orgs: str = ""
    # Fallback token pre orgy, ktoré nemajú vlastný GITHUB_TOKEN_<ORG>.
    github_token: str = ""

    database_url: str = "postgresql+psycopg://leaderboard:leaderboard@localhost:5432/leaderboard"
    sync_cron_hour: int = 3
    sync_cron_minute: int = 0
    sync_exclude_patterns: str = "*-config,tf-org,tf-infra"

    # Backfill window. 0 = celá história repa (od prvého commitu). Kladné číslo =
    # strop v dňoch, keby bola celá história pri veľkom orgu príliš drahá na
    # GraphQL rate limit.
    sync_history_days: int = 0

    # Inkrementálny sync znovu prečíta posledných N dní a prepíše dotknuté týždne.
    # Pokrýva force-push / rebase / commity dopísané spätne.
    sync_overlap_days: int = 21

    # Prihlásenie (HTTP Basic). Prázdne = appka je bez loginu.
    auth_user: str = ""
    auth_password: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    # Origins, ktoré smú volať /api/* z prehliadača (vlastný FE na inej doméne).
    # Prázdne = CORS vypnuté, API sa dá volať len zo servera / z tejto appky.
    api_cors_origins: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def exclude_patterns(self) -> list[str]:
        return [p.strip() for p in self.sync_exclude_patterns.split(",") if p.strip()]

    @property
    def org_names(self) -> list[str]:
        """Organizácie v poradí z configu, bez duplicít (case-insensitive)."""
        names = [o.strip() for o in self.github_orgs.split(",") if o.strip()]
        if self.github_org.strip():
            names.append(self.github_org.strip())
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            if name.lower() not in seen:
                seen.add(name.lower())
                unique.append(name)
        return unique

    def token_for(self, org: str) -> str:
        """Token organizácie; ak nemá vlastný, použije sa spoločný GITHUB_TOKEN."""
        return _env(token_env_key(org)) or self.github_token

    @property
    def orgs(self) -> list[OrgConfig]:
        return [OrgConfig(name, self.token_for(name)) for name in self.org_names]

    def org(self, name: str) -> OrgConfig | None:
        """Nájde nakonfigurovaný org podľa mena (case-insensitive)."""
        for cfg in self.orgs:
            if cfg.name.lower() == name.strip().lower():
                return cfg
        return None


settings = Settings()
