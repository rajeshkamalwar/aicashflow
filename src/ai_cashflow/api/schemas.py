"""API response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class Phase0SummaryResponse(BaseModel):
    marketplace_sample_files: int
    bank_payment_sample_files: int
    expected_payout_records: int
    marketplace_transaction_records: int
    bank_payment_receipt_records: int
    data_quality_issues: int
    matched_payouts: int
    unmatched_expected_payouts: int
    unmatched_bank_receipts: int
    total_expected_payouts: str
    marketplace_net_activity: str
    marketplace_order_amount: str
    marketplace_refund_amount: str
    marketplace_tax_amount: str
    marketplace_selling_fees: str
    marketplace_fba_fees: str
    marketplace_other_fees: str
    marketplace_reimbursements: str
    marketplace_withheld_tax: str
    marketplace_failed_transfers: str
    marketplace_reserve_adjustments: str
    marketplace_uncategorised: str
    marketplace_deferred_amount: str
    marketplace_released_amount: str
    total_bank_payment_receipts: str
    matched_amount: str
    unmatched_expected_amount: str
    unmatched_bank_payment_receipt_amount: str


class RunReportResponse(BaseModel):
    status: str
    reports_dir: str


class AmazonIntegrationUpdate(BaseModel):
    application_id: str = ""
    endpoint: str = "https://sellingpartnerapi-na.amazon.com"
    marketplace: str = "Canada"
    sync_frequency_minutes: int = Field(default=60, ge=5, le=10_080)
    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None


class AmazonSourceUpdate(AmazonIntegrationUpdate):
    name: str = Field(min_length=1, max_length=120)


class AmazonTransactionBackfillCreate(BaseModel):
    marketplace_id: str = Field(min_length=1, max_length=40)
    marketplace_name: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3)
    transaction_status: Literal["DEFERRED", "RELEASED", "DEFERRED_RELEASED"]
    overall_start: datetime
    overall_end: datetime
    slice_hours: int = Field(default=24, ge=1, le=720)
