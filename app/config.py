from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str = ""
    github_org: str = ""
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


settings = Settings()
