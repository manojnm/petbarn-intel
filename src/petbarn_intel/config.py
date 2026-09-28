"""Central configuration, loaded from environment variables / .env.

Every other module reads settings through `get_settings()` (a cached singleton)
rather than reading `os.environ` directly, so behaviour is testable and
overridable in one place.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM provider -----------------------------------------------------
    # "azure" today; "openai" is a drop-in swap of model strings only (see
    # docs/decisions/005-azure-openai-via-litellm.md).
    llm_provider: str = Field(default="azure")

    azure_openai_api_key: str = Field(default="")
    azure_openai_endpoint: str = Field(default="")
    azure_openai_api_version: str = Field(default="2024-10-21")
    azure_chat_deployment_mini: str = Field(default="gpt-4o-mini")
    azure_chat_deployment_main: str = Field(default="gpt-4o")
    azure_embedding_deployment: str = Field(default="text-embedding-3-small")
    # Embeddings deployment can be on a different API version than chat
    # (e.g. it was created before/after a chat-only API bump) -- kept
    # separate rather than reusing `azure_openai_api_version`.
    azure_embedding_api_version: str = Field(default="2024-10-21")

    openai_api_key: str = Field(default="")
    openai_chat_model_mini: str = Field(default="gpt-4o-mini")
    openai_chat_model_main: str = Field(default="gpt-4o")
    openai_embedding_model: str = Field(default="text-embedding-3-small")

    # --- Storage ------------------------------------------------------------
    data_dir: Path = Field(default=Path("./data"))
    db_path: Path = Field(default=Path("./data/db/petbarn_intel.sqlite3"))
    raw_cache_dir: Path = Field(default=Path("./data/raw"))

    # --- HTTP / scraping ------------------------------------------------------
    http_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128 Safari/537.36 PetbarnIntelBot/0.1 "
            "(+contact: research@example.com)"
        )
    )
    rate_limit_rps: float = Field(default=3.0)
    max_concurrency: int = Field(default=8)
    request_timeout_s: float = Field(default=20.0)
    enable_playwright: bool = Field(default=False)

    # --- Crawl scope ----------------------------------------------------------
    seed_size: int = Field(default=200)
    reviews_per_product: int = Field(default=150)
    census_page_size: int = Field(default=300)

    # --- Generic retailer adapter ----------------------------------------------
    allowlisted_domains: str = Field(
        default="petbarn.com.au,www.petbarn.com.au,petcircle.com.au,"
        "www.petcircle.com.au,budgetpetproducts.com.au,www.budgetpetproducts.com.au"
    )

    # --- Observability --------------------------------------------------------
    enable_langfuse: bool = Field(default=False)
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    # --- Agent ------------------------------------------------------------------
    agent_max_tool_calls: int = Field(default=12)
    agent_max_live_scrapes_per_turn: int = Field(default=3)
    agent_crawl_approval_threshold_pages: int = Field(default=20)

    # Tool-result cache (plan section 1.9): short-TTL, in-process, keyed by
    # (tool name, args) -- repeat catalog/review lookups within a turn (e.g.
    # two specialists in the same `Send` fan-out independently resolving the
    # same product) or across turns in one session cost nothing.
    tool_cache_ttl_seconds: int = Field(default=300)
    tool_cache_maxsize: int = Field(default=512)

    # Custom Azure deployment names (e.g. "gpt-5.4-mini-kai-dev") have no
    # entry in LiteLLM's price registry, so `agent/llm.py::estimate_cost`
    # falls back to pricing this public model name instead once the real
    # deployment lookup misses -- same underlying model, just costed under
    # its public name. Purely for the Ops page's cost estimate; never used
    # for the actual API call.
    cost_reference_model: str = Field(default="gpt-5.4-mini")

    @computed_field  # type: ignore[misc]
    @property
    def allowlisted_domains_set(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower() for d in self.allowlisted_domains.split(",") if d.strip()
        )

    @computed_field  # type: ignore[misc]
    @property
    def has_azure_credentials(self) -> bool:
        return bool(self.azure_openai_api_key and self.azure_openai_endpoint)

    @computed_field  # type: ignore[misc]
    @property
    def has_openai_credentials(self) -> bool:
        return bool(self.openai_api_key)

    @computed_field  # type: ignore[misc]
    @property
    def has_llm_credentials(self) -> bool:
        if self.llm_provider == "azure":
            return self.has_azure_credentials
        return self.has_openai_credentials

    def chat_model_id(self, role: str = "main") -> str:
        """Return a LiteLLM-style model string for the given role.

        role is one of "mini" (cheap/fast: guard, planner, verifier, extraction
        fallback) or "main" (synthesizer). Swapping providers is a one-line
        change here, never in call sites.
        """
        if self.llm_provider == "azure":
            deployment = (
                self.azure_chat_deployment_mini
                if role == "mini"
                else self.azure_chat_deployment_main
            )
            return f"azure/{deployment}"
        model = self.openai_chat_model_mini if role == "mini" else self.openai_chat_model_main
        return f"openai/{model}"

    def embedding_model_id(self) -> str:
        if self.llm_provider == "azure":
            return f"azure/{self.azure_embedding_deployment}"
        return f"openai/{self.openai_embedding_model}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
