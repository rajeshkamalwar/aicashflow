"""Application configuration for the Phase 0 backend."""

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"


@dataclass(frozen=True)
class BrandConfig:
    primary_color: str = "#087f7a"
    logo_text: str = "CC"


@dataclass(frozen=True)
class ReconciliationConfig:
    date_tolerance_days: int = 3
    amount_tolerance: Decimal = Decimal("0.00")
    usd_exchange_rates: dict[str, Decimal] = field(
        default_factory=lambda: {
            "USD": Decimal("1"),
            "CAD": Decimal("0.7130332048"),
            "MXN": Decimal("0.05738406337"),
            "AUD": Decimal("0.7008311942"),
            "JPY": Decimal("0.006132587773536212"),
            "SGD": Decimal("0.7716141252875118"),
        }
    )
    usd_exchange_rates_as_of: str = "2026-07-16"
    usd_exchange_rates_source: str = "Configured tenant FX table"
    usd_exchange_rates_max_age_days: int = 7


@dataclass(frozen=True)
class SourceConfig:
    name: str
    type: str
    status: str
    owner: str = "Finance owner"
    frequency: str = "Daily"
    evidence_type: str = "Source data"
    api_endpoint: str = ""
    credential_reference: str = ""
    # Bank-specific fields (ignored for marketplace sources)
    bank_name: str = ""
    account_reference: str = ""
    currency: str = ""
    connection_type: str = ""


@dataclass(frozen=True)
class SourcesConfig:
    marketplaces: list[SourceConfig] = field(default_factory=list)
    banks: list[SourceConfig] = field(default_factory=list)


@dataclass(frozen=True)
class EntityConfig:
    name: str
    currency: str
    status: str


@dataclass(frozen=True)
class PlannedOutflowConfig:
    name: str
    category: str
    amount: str
    frequency: str


@dataclass(frozen=True)
class AlertRuleConfig:
    name: str
    trigger: str
    recipient: str


@dataclass(frozen=True)
class UserRoleConfig:
    name: str
    role: str
    scope: str


@dataclass(frozen=True)
class GovernanceConfig:
    data_retention: str = "To be confirmed"
    hosting_region: str = "To be confirmed"
    ai_enabled: bool = False
    human_approval_required: bool = True


@dataclass(frozen=True)
class ClientSetupConfig:
    entities: list[EntityConfig] = field(default_factory=list)
    planned_outflows: list[PlannedOutflowConfig] = field(default_factory=list)
    forecast_horizons: list[str] = field(
        default_factory=lambda: ["Daily", "7 days", "14 days", "30 days", "60 days"]
    )
    alert_rules: list[AlertRuleConfig] = field(default_factory=list)
    user_roles: list[UserRoleConfig] = field(default_factory=list)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)


@dataclass(frozen=True)
class TenantConfig:
    product_name: str = "Cashflow Command Center"
    tenant_name: str = "Ergode Group"
    poc_entity: str = "Ergode Inc."
    demo_mode: bool = False
    brand: BrandConfig = field(default_factory=BrandConfig)
    currencies: list[str] = field(default_factory=lambda: ["USD"])
    reconciliation: ReconciliationConfig = field(default_factory=ReconciliationConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    client_setup: ClientSetupConfig = field(default_factory=ClientSetupConfig)

    @classmethod
    def default(cls) -> "TenantConfig":
        return cls(
            tenant_name="Ergode Group",
            poc_entity="Ergode Inc.",
            brand=BrandConfig(logo_text="CC"),
            sources=SourcesConfig(
                marketplaces=[
                    SourceConfig(
                        name="Amazon Seller Central - FastMediaMX Canada",
                        type="Marketplace portal report",
                        status="Awaiting actual upload",
                        owner="Marketplace operations",
                        frequency="Daily",
                        evidence_type="Amazon Payments transaction report",
                        api_endpoint="",
                        credential_reference="",
                    ),
                    SourceConfig(
                        name="Amazon SP-API - FastMediaMX Canada",
                        type="Marketplace API",
                        status="Developer registration under review",
                        owner="Finance analyst",
                        frequency="Pending approval",
                        evidence_type="SP-API authorization",
                        api_endpoint="",
                        credential_reference="",
                    ),
                ],
                banks=[],
            )
            ,
            client_setup=ClientSetupConfig(
                entities=[
                    EntityConfig(
                        name="Ergode Inc.",
                        currency="USD",
                        status="POC entity",
                    )
                ],
                planned_outflows=[
                    PlannedOutflowConfig(
                        name="Vendor payments",
                        category="Operating outflow",
                        amount="To be confirmed",
                        frequency="Weekly",
                    ),
                    PlannedOutflowConfig(
                        name="Payroll",
                        category="Payroll",
                        amount="To be confirmed",
                        frequency="Monthly",
                    ),
                ],
                alert_rules=[
                    AlertRuleConfig(
                        name="Delayed payout",
                        trigger="Expected receipt is past tolerance window",
                        recipient="Finance owner",
                    ),
                    AlertRuleConfig(
                        name="Low bank balance",
                        trigger="Available balance below threshold",
                        recipient="CFO office",
                    ),
                ],
                user_roles=[
                    UserRoleConfig(
                        name="CFO Office",
                        role="Approver",
                        scope="All entities",
                    ),
                    UserRoleConfig(
                        name="Finance Analyst",
                        role="Reviewer",
                        scope="Assigned entities",
                    ),
                ],
            )
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "TenantConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "TenantConfig":
        brand_payload = _dict(payload.get("brand"))
        reconciliation_payload = _dict(payload.get("reconciliation"))
        sources_payload = _dict(payload.get("sources"))
        client_setup_payload = _dict(payload.get("client_setup"))
        default_client_setup = cls.default().client_setup
        governance_payload = _dict(client_setup_payload.get("governance"))
        return cls(
            product_name=str(payload.get("product_name", cls.product_name)),
            tenant_name=str(payload.get("tenant_name", cls.tenant_name)),
            poc_entity=str(payload.get("poc_entity", cls.poc_entity)),
            demo_mode=bool(payload.get("demo_mode", cls.demo_mode)),
            brand=BrandConfig(
                primary_color=str(
                    brand_payload.get("primary_color", BrandConfig.primary_color)
                ),
                logo_text=str(brand_payload.get("logo_text", BrandConfig.logo_text)),
            ),
            currencies=[str(currency) for currency in payload.get("currencies", ["USD"])],
            reconciliation=ReconciliationConfig(
                date_tolerance_days=int(
                    reconciliation_payload.get("date_tolerance_days", 3)
                ),
                amount_tolerance=Decimal(
                    str(reconciliation_payload.get("amount_tolerance", "0.00"))
                ),
                usd_exchange_rates={
                    str(currency).upper(): Decimal(str(rate))
                    for currency, rate in _dict(
                        reconciliation_payload.get("usd_exchange_rates")
                    ).items()
                }
                or ReconciliationConfig().usd_exchange_rates,
                usd_exchange_rates_as_of=str(
                    reconciliation_payload.get(
                        "usd_exchange_rates_as_of",
                        ReconciliationConfig.usd_exchange_rates_as_of,
                    )
                ),
                usd_exchange_rates_source=str(
                    reconciliation_payload.get(
                        "usd_exchange_rates_source",
                        ReconciliationConfig.usd_exchange_rates_source,
                    )
                ),
                usd_exchange_rates_max_age_days=max(
                    0,
                    int(reconciliation_payload.get(
                        "usd_exchange_rates_max_age_days",
                        ReconciliationConfig.usd_exchange_rates_max_age_days,
                    )),
                ),
            ),
            sources=SourcesConfig(
                marketplaces=[
                    SourceConfig(
                        name=str(item.get("name", "")),
                        type=str(item.get("type", "")),
                        status=str(item.get("status", "")),
                        owner=str(item.get("owner", "Finance owner")),
                        frequency=str(item.get("frequency", "Daily")),
                        evidence_type=str(item.get("evidence_type", "Source data")),
                        api_endpoint=str(item.get("api_endpoint", "")),
                        credential_reference=str(
                            item.get("credential_reference", "")
                        ),
                    )
                    for item in _list_of_dicts(sources_payload.get("marketplaces"))
                ],
                banks=[
                    SourceConfig(
                        name=str(item.get("name", "")),
                        type=str(item.get("type", "")),
                        status=str(item.get("status", "")),
                        owner=str(item.get("owner", "Finance owner")),
                        frequency=str(item.get("frequency", "Daily")),
                        evidence_type=str(item.get("evidence_type", "Source data")),
                        bank_name=str(item.get("bank_name", "")),
                        account_reference=str(item.get("account_reference", "")),
                        currency=str(item.get("currency", "")),
                        connection_type=str(item.get("connection_type", "")),
                        api_endpoint=str(item.get("api_endpoint", "")),
                        credential_reference=str(
                            item.get("credential_reference", "")
                        ),
                    )
                    for item in _list_of_dicts(sources_payload.get("banks"))
                ],
            ),
            client_setup=ClientSetupConfig(
                entities=[
                    EntityConfig(
                        name=str(item.get("name", "")),
                        currency=str(item.get("currency", "")),
                        status=str(item.get("status", "")),
                    )
                    for item in _list_of_dicts(
                        client_setup_payload.get(
                            "entities",
                            [
                                entity.__dict__
                                for entity in default_client_setup.entities
                            ],
                        )
                    )
                ],
                planned_outflows=[
                    PlannedOutflowConfig(
                        name=str(item.get("name", "")),
                        category=str(item.get("category", "")),
                        amount=str(item.get("amount", "")),
                        frequency=str(item.get("frequency", "")),
                    )
                    for item in _list_of_dicts(
                        client_setup_payload.get(
                            "planned_outflows",
                            [
                                outflow.__dict__
                                for outflow in default_client_setup.planned_outflows
                            ],
                        )
                    )
                ],
                forecast_horizons=[
                    str(item)
                    for item in client_setup_payload.get(
                        "forecast_horizons",
                        default_client_setup.forecast_horizons,
                    )
                    if str(item)
                ],
                alert_rules=[
                    AlertRuleConfig(
                        name=str(item.get("name", "")),
                        trigger=str(item.get("trigger", "")),
                        recipient=str(item.get("recipient", "")),
                    )
                    for item in _list_of_dicts(
                        client_setup_payload.get(
                            "alert_rules",
                            [
                                rule.__dict__
                                for rule in default_client_setup.alert_rules
                            ],
                        )
                    )
                ],
                user_roles=[
                    UserRoleConfig(
                        name=str(item.get("name", "")),
                        role=str(item.get("role", "")),
                        scope=str(item.get("scope", "")),
                    )
                    for item in _list_of_dicts(
                        client_setup_payload.get(
                            "user_roles",
                            [
                                role.__dict__
                                for role in default_client_setup.user_roles
                            ],
                        )
                    )
                ],
                governance=GovernanceConfig(
                    data_retention=str(
                        governance_payload.get("data_retention", "To be confirmed")
                    ),
                    hosting_region=str(
                        governance_payload.get("hosting_region", "To be confirmed")
                    ),
                    ai_enabled=bool(governance_payload.get("ai_enabled", False)),
                    human_approval_required=bool(
                        governance_payload.get("human_approval_required", True)
                    ),
                ),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "product_name": self.product_name,
            "tenant_name": self.tenant_name,
            "poc_entity": self.poc_entity,
            "demo_mode": self.demo_mode,
            "brand": {
                "primary_color": self.brand.primary_color,
                "logo_text": self.brand.logo_text,
            },
            "currencies": self.currencies,
            "reconciliation": {
                "date_tolerance_days": self.reconciliation.date_tolerance_days,
                "amount_tolerance": str(self.reconciliation.amount_tolerance),
                "usd_exchange_rates": {
                    currency: str(rate)
                    for currency, rate in self.reconciliation.usd_exchange_rates.items()
                },
                "usd_exchange_rates_as_of": (
                    self.reconciliation.usd_exchange_rates_as_of
                ),
                "usd_exchange_rates_source": (
                    self.reconciliation.usd_exchange_rates_source
                ),
                "usd_exchange_rates_max_age_days": (
                    self.reconciliation.usd_exchange_rates_max_age_days
                ),
            },
            "sources": {
                "marketplaces": [
                    source.__dict__ for source in self.sources.marketplaces
                ],
                "banks": [source.__dict__ for source in self.sources.banks],
            },
            "client_setup": {
                "entities": [entity.__dict__ for entity in self.client_setup.entities],
                "planned_outflows": [
                    outflow.__dict__
                    for outflow in self.client_setup.planned_outflows
                ],
                "forecast_horizons": self.client_setup.forecast_horizons,
                "alert_rules": [
                    rule.__dict__ for rule in self.client_setup.alert_rules
                ],
                "user_roles": [role.__dict__ for role in self.client_setup.user_roles],
                "governance": {
                    "data_retention": self.client_setup.governance.data_retention,
                    "hosting_region": self.client_setup.governance.hosting_region,
                    "ai_enabled": self.client_setup.governance.ai_enabled,
                    "human_approval_required": (
                        self.client_setup.governance.human_approval_required
                    ),
                },
            },
        }

    def save(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )


@dataclass
class AppConfig:
    samples_dir: Path = field(default_factory=lambda: _path_env(
        "AI_CASHFLOW_SAMPLES_DIR", ROOT_DIR / "data" / "samples"
    ))
    reports_dir: Path = field(default_factory=lambda: _path_env(
        "AI_CASHFLOW_REPORTS_DIR", ROOT_DIR / "reports" / "phase0"
    ))
    marketplace_ar_registry_path: Path = field(default_factory=lambda: _path_env(
        "AI_CASHFLOW_MARKETPLACE_AR_REGISTRY_PATH", CONFIG_DIR / "marketplace_ar_sources.csv"
    ))
    marketplace_ar_fx_path: Path = field(default_factory=lambda: _path_env(
        "AI_CASHFLOW_MARKETPLACE_AR_FX_PATH", CONFIG_DIR / "marketplace_ar_fx_rates.csv"
    ))
    marketplace_ar_ingest_mode: str = field(default_factory=lambda: os.getenv(
        "AI_CASHFLOW_MARKETPLACE_AR_INGEST_MODE", "hybrid"
    ).strip().lower())
    tenant: TenantConfig = field(default_factory=lambda: load_tenant_config())
    tenant_config_path: Path = field(default_factory=lambda: _path_env(
        "AI_CASHFLOW_TENANT_CONFIG_PATH", CONFIG_DIR / "tenant.local.json"
    ))
    database_path: Path | None = None
    environment: str = field(default_factory=lambda: os.getenv(
        "AI_CASHFLOW_ENV", "local"
    ))
    api_key: str | None = field(default_factory=lambda: os.getenv(
        "AI_CASHFLOW_API_KEY"
    ))
    transaction_visibility_enabled: bool = field(default_factory=lambda: os.getenv(
        "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"})
    max_upload_bytes: int = field(default_factory=lambda: _int_env(
        "AI_CASHFLOW_MAX_UPLOAD_BYTES",
        10 * 1024 * 1024,
    ))
    allowed_upload_categories: tuple[str, ...] = (
        "manual_uploads",
        "marketplaces",
        "marketplaces/api",
        "marketplaces/portal_reports",
        "marketplaces/emails",
    )

    def __post_init__(self) -> None:
        if self.marketplace_ar_ingest_mode not in {"hybrid", "api_only"}:
            raise ValueError(
                "AI_CASHFLOW_MARKETPLACE_AR_INGEST_MODE must be hybrid or api_only"
            )
        if self.database_path is None:
            self.database_path = _path_env(
                "AI_CASHFLOW_DATABASE_PATH",
                self.reports_dir / "operations.sqlite3",
            )

    @property
    def supported_currencies(self) -> set[str]:
        return set(self.tenant.currencies)

    @property
    def date_tolerance_days(self) -> int:
        return self.tenant.reconciliation.date_tolerance_days

    @property
    def amount_tolerance(self) -> Decimal:
        return self.tenant.reconciliation.amount_tolerance

    @property
    def storage_backend(self) -> str:
        return "sqlite"


def load_tenant_config() -> TenantConfig:
    local_path = _path_env(
        "AI_CASHFLOW_TENANT_CONFIG_PATH", CONFIG_DIR / "tenant.local.json"
    )
    example_path = CONFIG_DIR / "tenant.example.json"
    if local_path.exists():
        return TenantConfig.from_file(local_path)
    if example_path.exists():
        return TenantConfig.from_file(example_path)
    return TenantConfig.default()


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)
