from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="/etc/meetingsai/meetingsai.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8095
    app_base_url: str = "http://127.0.0.1:8095"

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "meetingsai"
    db_user: str = "meetingsai_app"
    db_password: str = ""

    auth_password_hash: str = ""
    session_secret: str = ""

    # Phase 8 aggregation tuning.  Defaults are safe; values are overridable
    # via env if the environment file provides them.
    aggregation_window_seconds: int = 30
    aggregation_scheduler_interval_seconds: int = 10
    aggregation_max_segments_per_run: int = 500

    # Phase 9 analysis tuning.  Same pattern as aggregation:
    # defaults are safe, values are overridable via env.
    analysis_model: str = "gpt-4o-mini"
    analysis_min_interval_seconds: int = 60
    analysis_recent_discussion_segments: int = 20
    # Optional override for the one-time finalization pass.  Empty
    # string means fall back to analysis_model.  Set FINAL_ANALYSIS_MODEL
    # in env to run finalization on a stronger model than rolling
    # analysis.  Read at call time by finalizer.py.
    final_analysis_model: str = ""

    # Phase G4: Context Review tuning.  Same pattern as the rest:
    # defaults are safe; values are overridable via env.
    context_review_enabled: bool = True
    context_review_interval_seconds: int = 300
    context_review_min_new_segments: int = 4
    context_review_recent_discussions: int = 20
    context_review_recent_transcripts: int = 60
    context_review_model: str = "gpt-4o-mini"

    # Stage A Topic Intelligence.  Defaults are safe: the scheduler is
    # disabled unless explicitly enabled via env.  Values are overridable
    # via the environment file.
    topic_intelligence_enabled: bool = False
    topic_intelligence_interval_seconds: int = 120
    topic_intelligence_model: str = "gpt-4o"

    # Stage C safeguards — defaults safe; overridden by env for the test.
    topic_intelligence_test_mode: bool = False
    topic_intelligence_test_meeting_id: str = ""
    topic_intelligence_max_completion_tokens: int = 1500
    topic_intelligence_max_segment_chars: int = 4000
    topic_intelligence_max_topics_per_meeting: int = 20
    topic_intelligence_max_provider_calls_per_meeting: int = 200

    # Phase 9: model API key.  Read from env; never logged.
    # Phase 9: model API key. Read from /etc/meetingsai/meetingsai.env.
    # Never hard-code this. Never commit this.
    openai_api_key: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
