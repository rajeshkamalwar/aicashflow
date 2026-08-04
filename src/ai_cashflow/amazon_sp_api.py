"""Read-only access to Amazon SP-API settlement-report metadata."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import gzip
from urllib.parse import urlparse

import httpx


SETTLEMENT_REPORT_TYPE = "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
MAX_REPORT_BYTES = 25 * 1024 * 1024
MONEY_QUANTUM = Decimal("0.01")


class AmazonSpApiError(RuntimeError):
    """An Amazon response that is safe to surface without exposing credentials."""


@dataclass(frozen=True)
class AmazonSpApiConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    endpoint: str = "https://sellingpartnerapi-na.amazon.com"

    @property
    def missing(self) -> tuple[str, ...]:
        names = (
            ("client ID", self.client_id),
            ("client secret", self.client_secret),
            ("refresh token", self.refresh_token),
        )
        return tuple(name for name, value in names if not value)

    def validate(self) -> None:
        if self.missing:
            missing = ", ".join(self.missing)
            raise ValueError(f"Amazon SP-API configuration missing: {missing}.")
        if not self.endpoint.startswith("https://sellingpartnerapi-") or not self.endpoint.endswith(".amazon.com"):
            raise ValueError("Amazon SP-API endpoint must be an official HTTPS endpoint.")


class AmazonSpApiClient:
    def __init__(self, config: AmazonSpApiConfig, *, http: httpx.Client | None = None) -> None:
        self.config = config
        self.http = http

    def list_settlement_reports(self) -> list[dict[str, object]]:
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                return self._list_settlement_reports(http)
        return self._list_settlement_reports(self.http)

    def probe_payments_access(self) -> list[dict[str, object]]:
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                return self._probe_payments_access(http)
        return self._probe_payments_access(self.http)

    def list_financial_event_groups(
        self, started_after: datetime
    ) -> list[dict[str, object]]:
        if started_after.tzinfo is None:
            started_after = started_after.replace(tzinfo=timezone.utc)
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                token = self._access_token(http)
                return self._list_financial_event_groups(http, token, started_after)
        token = self._access_token(self.http)
        return self._list_financial_event_groups(self.http, token, started_after)

    def get_current_balances(
        self,
        usd_rates: dict[str, Decimal],
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return deduplicated Amazon open balances, preserving each currency."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                return self._get_current_balances(http, usd_rates, current_time)
        return self._get_current_balances(self.http, usd_rates, current_time)

    def _get_current_balances(
        self,
        http: httpx.Client,
        usd_rates: dict[str, Decimal],
        now: datetime,
    ) -> dict[str, object]:
        self.config.validate()
        token = self._access_token(http)
        groups = self._list_financial_event_groups(
            http, token, now - timedelta(days=90)
        )
        seen: set[str] = set()
        balances = []
        totals: dict[str, Decimal] = {}
        total_usd = Decimal("0")
        for group in groups:
            if str(group.get("ProcessingStatus", "")).lower() != "open":
                continue
            group_id = str(group.get("FinancialEventGroupId", "")).strip()
            if not group_id:
                raise AmazonSpApiError("Amazon returned an open balance without an event group ID.")
            if group_id in seen:
                continue
            seen.add(group_id)
            amount, currency = self._money(group.get("OriginalTotal"), "open financial event group total")
            rate = usd_rates.get(currency)
            if rate is None or rate <= 0:
                raise AmazonSpApiError(f"No positive USD exchange rate is configured for {currency}.")
            usd_amount = (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            totals[currency] = totals.get(currency, Decimal("0")) + amount
            total_usd += usd_amount
            balances.append({
                "financial_event_group_id": group_id,
                "currency": currency,
                "amount": self._format_money(amount),
                "usd_rate": str(rate),
                "usd_amount": self._format_money(usd_amount),
                "started_at": group.get("FinancialEventGroupStart"),
                "processing_status": "Open",
            })
        if not balances:
            raise AmazonSpApiError("Amazon did not return a current open financial event group.")
        retrieved_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        financial_values = [
            self._financial_value(
                value_type="OPEN_BALANCE",
                amount=Decimal(balance["amount"]),
                currency=str(balance["currency"]),
                source_field="OriginalTotal",
                processing_status="Open",
                retrieved_at=retrieved_at,
                effective_at=balance.get("started_at"),
                is_authoritative=True,
                authoritative_for="AMAZON_REPORTED_OPEN_BALANCE",
                calculation_method="DIRECT_SP_API_FIELD",
            )
            for balance in balances
        ]
        unavailable_reason = (
            "Amazon SP-API has not supplied an authoritative current reserve, "
            "reserve-adjusted funds, or upcoming payout."
        )
        for currency in sorted(totals):
            for value_type in (
                "CURRENT_RESERVE",
                "RESERVE_ADJUSTED_FUNDS",
                "UPCOMING_PAYOUT",
            ):
                financial_values.append(self._financial_value(
                    value_type=value_type,
                    amount=None,
                    currency=currency,
                    source_field=None,
                    processing_status=None,
                    retrieved_at=retrieved_at,
                    effective_at=None,
                    is_authoritative=False,
                    authoritative_for=None,
                    calculation_method=None,
                    availability_reason=unavailable_reason,
                ))
        return {
            "status": "ready",
            "mode": "amazon_open_balances",
            "currency": "USD",
            "as_of": retrieved_at,
            "group_count": len(balances),
            "balances": balances,
            "totals_by_currency": {
                currency: self._format_money(amount)
                for currency, amount in sorted(totals.items())
            },
            "amounts": {"current_balance": self._format_money(total_usd)},
            "expected_payout_forecasts": [],
            "financial_values": financial_values,
            "validation": {
                "status": "passed",
                "basis": "API structural validation only",
                "deduplicated_by": "FinancialEventGroupId",
            },
            "api_data_validation": {
                "status": "passed",
                "records_parsed": len(balances),
                "deduplicated_by": "FinancialEventGroupId",
            },
        }

    def list_payment_transactions(
        self,
        marketplace_id: str,
        financial_event_group_id: str,
        *,
        posted_after: datetime,
    ) -> list[dict[str, object]]:
        if not marketplace_id or not financial_event_group_id:
            raise ValueError("Marketplace and financial event group IDs are required.")
        if posted_after.tzinfo is None:
            posted_after = posted_after.replace(tzinfo=timezone.utc)
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                token = self._access_token(http)
                return self._list_transactions(
                    http,
                    token,
                    marketplace_id=marketplace_id,
                    posted_after=posted_after,
                    transaction_status="RELEASED",
                    financial_event_group_id=financial_event_group_id,
                )
        token = self._access_token(self.http)
        return self._list_transactions(
            self.http,
            token,
            marketplace_id=marketplace_id,
            posted_after=posted_after,
            transaction_status="RELEASED",
            financial_event_group_id=financial_event_group_id,
        )

    def get_financial_position(
        self,
        marketplace_id: str,
        usd_rate: Decimal,
        *,
        expected_currency: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return the current Seller Central statement position, normalized to USD."""
        if not marketplace_id:
            raise ValueError("An Amazon marketplace ID is required.")
        if usd_rate <= 0:
            raise ValueError("The USD exchange rate must be positive.")
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if self.http is None:
            with httpx.Client(timeout=30.0) as http:
                return self._get_financial_position(
                    http, marketplace_id, usd_rate, current_time, expected_currency
                )
        return self._get_financial_position(
            self.http, marketplace_id, usd_rate, current_time, expected_currency
        )

    def _get_financial_position(
        self,
        http: httpx.Client,
        marketplace_id: str,
        usd_rate: Decimal,
        now: datetime,
        expected_currency: str | None,
    ) -> dict[str, object]:
        self.config.validate()
        token = self._access_token(http)
        groups = self._list_financial_event_groups(
            http,
            token,
            now - timedelta(days=90),
        )
        open_groups = [
            row for row in groups
            if str(row.get("ProcessingStatus", "")).lower() == "open"
            and (
                not expected_currency
                or self._money(
                    row.get("OriginalTotal"), "open financial event group total"
                )[1] == expected_currency
            )
        ]
        if not open_groups:
            raise AmazonSpApiError(
                "Amazon did not return a current open financial event group"
                f"{f' for {expected_currency}' if expected_currency else ''}."
            )
        open_group = max(
            open_groups,
            key=lambda row: str(row.get("FinancialEventGroupStart", "")),
        )
        closed_groups = [
            row for row in groups
            if str(row.get("ProcessingStatus", "")).lower() == "closed"
            and (
                not expected_currency
                or self._money(
                    row.get("OriginalTotal"), "closed financial event group total"
                )[1] == expected_currency
            )
        ]
        latest_closed = max(
            closed_groups,
            key=lambda row: str(
                row.get("FundTransferDate")
                or row.get("FinancialEventGroupEnd")
                or row.get("FinancialEventGroupStart")
                or ""
            ),
            default=None,
        )

        standard_balance, source_currency = self._money(
            open_group.get("OriginalTotal"),
            "open financial event group total",
        )
        beginning_balance, beginning_currency = self._money(
            open_group.get("BeginningBalance"),
            "open financial event group beginning balance",
        )
        self._require_currency(source_currency, beginning_currency)

        deferred_transactions = self._list_transactions(
            http,
            token,
            marketplace_id=marketplace_id,
            posted_after=now - timedelta(days=179),
            transaction_status="DEFERRED",
        )
        deferred_balance = self._sum_transactions(
            deferred_transactions,
            source_currency,
        )

        period_start = str(open_group.get("FinancialEventGroupStart", ""))
        if not period_start:
            raise AmazonSpApiError(
                "Amazon did not return the current settlement period start."
            )
        recent_payout = Decimal("0")
        recent_payout_date = None
        recent_payout_status = None
        if latest_closed is not None:
            recent_payout, recent_currency = self._money(
                latest_closed.get("OriginalTotal"),
                "latest closed financial event group total",
            )
            self._require_currency(source_currency, recent_currency)
            recent_payout_date = latest_closed.get("FundTransferDate")
            recent_payout_status = latest_closed.get("FundTransferStatus")

        source_amounts = {
            "standard_balance": standard_balance,
            "deferred_balance": deferred_balance,
            "total_balance": standard_balance + deferred_balance,
            "funds_available": None,
            "beginning_balance": beginning_balance,
            "reserve_balance": None,
            "recent_payout": recent_payout,
        }
        retrieved_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        unavailable_reason = (
            "Amazon SP-API has not supplied an authoritative upcoming payout "
            "amount and transfer date."
        )
        financial_values = [
            self._financial_value(
                value_type="OPEN_BALANCE",
                amount=standard_balance,
                currency=source_currency,
                source_field="OriginalTotal",
                processing_status="Open",
                retrieved_at=retrieved_at,
                effective_at=period_start,
                is_authoritative=True,
                authoritative_for="AMAZON_REPORTED_OPEN_BALANCE",
                calculation_method="DIRECT_SP_API_FIELD",
            ),
            self._financial_value(
                value_type="BEGINNING_BALANCE",
                amount=beginning_balance,
                currency=source_currency,
                source_field="BeginningBalance",
                processing_status="Open",
                retrieved_at=retrieved_at,
                effective_at=period_start,
                is_authoritative=True,
                authoritative_for="AMAZON_REPORTED_BEGINNING_BALANCE",
                calculation_method="DIRECT_SP_API_FIELD",
            ),
            self._financial_value(
                value_type="DEFERRED_BALANCE",
                amount=deferred_balance,
                currency=source_currency,
                source_field="listTransactions.totalAmount",
                processing_status="Deferred",
                retrieved_at=retrieved_at,
                effective_at=None,
                is_authoritative=True,
                authoritative_for="AMAZON_REPORTED_DEFERRED_BALANCE",
                calculation_method="SUM_DEFERRED_TRANSACTIONS",
            ),
            self._financial_value(
                value_type="TOTAL_BALANCE",
                amount=standard_balance + deferred_balance,
                currency=source_currency,
                source_field=None,
                processing_status=None,
                retrieved_at=retrieved_at,
                effective_at=period_start,
                is_authoritative=False,
                authoritative_for="APPLICATION_CALCULATED_TOTAL_BALANCE",
                calculation_method="OPEN_BALANCE_PLUS_DEFERRED_BALANCE",
            ),
        ]
        for value_type, reason in (
            (
                "CURRENT_RESERVE",
                "Amazon SP-API has not provided an authoritative current reserve value.",
            ),
            (
                "RESERVE_ADJUSTED_FUNDS",
                "Funds available cannot be calculated without an authoritative current reserve.",
            ),
            ("UPCOMING_PAYOUT", unavailable_reason),
        ):
            financial_values.append(self._financial_value(
                value_type=value_type,
                amount=None,
                currency=source_currency,
                source_field=None,
                processing_status=None,
                retrieved_at=retrieved_at,
                effective_at=None,
                is_authoritative=False,
                authoritative_for=None,
                calculation_method=None,
                availability_reason=reason,
            ))
        if latest_closed is not None:
            financial_values.append(self._financial_value(
                value_type="COMPLETED_PAYOUT",
                amount=recent_payout,
                currency=source_currency,
                source_field="OriginalTotal",
                processing_status="Closed",
                retrieved_at=retrieved_at,
                effective_at=recent_payout_date,
                is_authoritative=True,
                authoritative_for="AMAZON_REPORTED_COMPLETED_PAYOUT",
                calculation_method="DIRECT_SP_API_FIELD",
            ))
        return {
            "status": "ready",
            "currency": "USD",
            "source_currency": source_currency,
            "usd_rate": str(usd_rate),
            "as_of": retrieved_at,
            "settlement_period_start": period_start,
            "recent_payout_date": recent_payout_date,
            "recent_payout_status": recent_payout_status,
            "deferred_transaction_count": len(deferred_transactions),
            "funds_available_status": "unavailable",
            "funds_available_note": (
                "Amazon SP-API does not provide the current account-level reserve."
            ),
            "source_amounts": {
                name: self._format_money(value) if value is not None else None
                for name, value in source_amounts.items()
            },
            "amounts": {
                name: self._format_money(value * usd_rate) if value is not None else None
                for name, value in source_amounts.items()
            },
            "expected_payout_forecast": {
                "status": "unavailable",
                "availability_reason": unavailable_reason,
                "scheduled_date": None,
                "source_currency": source_currency,
                "currency": "USD",
                "source_amounts": {
                    name: None
                    for name in ("this_week", "next_week", "all_upcoming", "unscheduled")
                },
                "amounts": {
                    name: None
                    for name in ("this_week", "next_week", "all_upcoming", "unscheduled")
                },
            },
            "financial_values": financial_values,
            "api_data_validation": {
                "status": "passed",
                "records_parsed": len(deferred_transactions) + 1,
                "deduplicated_by": "transactionId and FinancialEventGroupId",
            },
        }

    def _list_financial_event_groups(
        self,
        http: httpx.Client,
        token: str,
        started_after: datetime,
    ) -> list[dict[str, object]]:
        params: dict[str, object] = {
            "FinancialEventGroupStartedAfter": (
                started_after.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "MaxResultsPerPage": 100,
        }
        rows = []
        seen_tokens: set[str] = set()
        for _ in range(100):
            response = http.get(
                f"{self.config.endpoint}/finances/v0/financialEventGroups",
                headers={"x-amz-access-token": token},
                params=params,
            )
            self._raise_for_status(response, "list financial event groups")
            payload = response.json().get("payload")
            groups = (
                payload.get("FinancialEventGroupList")
                if isinstance(payload, dict) else None
            )
            if not isinstance(groups, list):
                raise AmazonSpApiError(
                    "Amazon returned an invalid financial event group response."
                )
            rows.extend(row for row in groups if isinstance(row, dict))
            next_token = payload.get("NextToken")
            if not isinstance(next_token, str) or not next_token:
                return rows
            if next_token in seen_tokens:
                raise AmazonSpApiError(
                    "Amazon repeated a financial event group page token."
                )
            seen_tokens.add(next_token)
            params = {"NextToken": next_token}
        raise AmazonSpApiError(
            "Amazon financial event group pagination exceeded 100 pages."
        )

    def _list_transactions(
        self,
        http: httpx.Client,
        token: str,
        *,
        marketplace_id: str,
        posted_after: datetime | str,
        transaction_status: str | None = None,
        financial_event_group_id: str | None = None,
    ) -> list[dict[str, object]]:
        if isinstance(posted_after, datetime):
            posted_after_text = (
                posted_after.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        else:
            posted_after_text = posted_after
        params: dict[str, object] = {
            "postedAfter": posted_after_text,
            "marketplaceId": marketplace_id,
            "pageSize": 500,
        }
        if transaction_status:
            params["transactionStatus"] = transaction_status
        if financial_event_group_id:
            params["relatedIdentifierName"] = "FINANCIAL_EVENT_GROUP_ID"
            params["relatedIdentifierValue"] = financial_event_group_id

        rows: list[dict[str, object]] = []
        seen_transaction_ids: set[str] = set()
        next_token = None
        seen_tokens: set[str] = set()
        for _ in range(100):
            page_params = dict(params)
            if next_token:
                page_params["nextToken"] = next_token
            response = http.get(
                f"{self.config.endpoint}/finances/2024-06-19/transactions",
                headers={"x-amz-access-token": token},
                params=page_params,
            )
            self._raise_for_status(response, "list finance transactions")
            payload = response.json().get("payload")
            if not isinstance(payload, dict):
                raise AmazonSpApiError(
                    "Amazon returned an invalid finance transaction response."
                )
            transactions = payload.get("transactions", [])
            if not isinstance(transactions, list):
                raise AmazonSpApiError(
                    "Amazon returned an invalid finance transaction list."
                )
            for row in transactions:
                if not isinstance(row, dict):
                    continue
                transaction_id = str(row.get("transactionId", "")).strip()
                if transaction_id and transaction_id in seen_transaction_ids:
                    continue
                if transaction_id:
                    seen_transaction_ids.add(transaction_id)
                rows.append(row)
            next_token = payload.get("nextToken")
            if not isinstance(next_token, str) or not next_token:
                return rows
            if next_token in seen_tokens:
                raise AmazonSpApiError(
                    "Amazon repeated a finance transaction page token."
                )
            seen_tokens.add(next_token)
        raise AmazonSpApiError("Amazon finance transaction pagination exceeded 100 pages.")

    def _sum_transactions(
        self,
        transactions: list[dict[str, object]],
        expected_currency: str,
    ) -> Decimal:
        total = Decimal("0")
        for transaction in transactions:
            amount, currency = self._money(
                transaction.get("totalAmount"),
                "transaction total",
            )
            self._require_currency(expected_currency, currency)
            total += amount
        return total

    @staticmethod
    def _money(value: object, label: str) -> tuple[Decimal, str]:
        if not isinstance(value, dict):
            raise AmazonSpApiError(f"Amazon did not return a valid {label}.")
        amount_value = value.get("CurrencyAmount", value.get("currencyAmount"))
        currency = str(
            value.get("CurrencyCode", value.get("currencyCode", ""))
        ).upper()
        try:
            amount = Decimal(str(amount_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AmazonSpApiError(
                f"Amazon did not return a valid amount for {label}."
            ) from exc
        if not currency:
            raise AmazonSpApiError(
                f"Amazon did not return a currency for {label}."
            )
        return amount, currency

    @staticmethod
    def _require_currency(expected: str, actual: str) -> None:
        if expected != actual:
            raise AmazonSpApiError(
                "Amazon returned mixed currencies for one marketplace position."
            )

    @staticmethod
    def _format_money(value: Decimal) -> str:
        return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))

    @classmethod
    def _financial_value(
        cls,
        *,
        value_type: str,
        amount: Decimal | None,
        currency: str | None,
        source_field: str | None,
        processing_status: str | None,
        retrieved_at: str,
        effective_at: object,
        is_authoritative: bool,
        authoritative_for: str | None,
        calculation_method: str | None,
        availability_reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "type": value_type,
            "amount": cls._format_money(amount) if amount is not None else None,
            "currency": currency,
            "source": "AMAZON_SP_API",
            "sourceField": source_field,
            "processingStatus": processing_status,
            "retrievedAt": retrieved_at,
            "effectiveAt": effective_at,
            "isAuthoritative": is_authoritative,
            "authoritativeFor": authoritative_for,
            "calculationMethod": calculation_method,
            "availabilityReason": availability_reason,
        }

    def _probe_payments_access(self, http: httpx.Client) -> list[dict[str, object]]:
        self.config.validate()
        response = http.get(
            f"{self.config.endpoint}/finances/2024-06-19/transactions",
            headers={"x-amz-access-token": self._access_token(http)},
            params={
                "postedAfter": (
                    (datetime.now(timezone.utc) - timedelta(days=179))
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "pageSize": 1,
            },
        )
        self._raise_for_status(response, "probe finance transactions")
        payload = response.json().get("payload")
        transactions = payload.get("transactions") if isinstance(payload, dict) else None
        if not isinstance(transactions, list):
            raise AmazonSpApiError("Amazon returned an invalid finance transaction response.")
        return [row for row in transactions if isinstance(row, dict)]

    def download_report_document(self, report_document_id: str) -> str:
        if self.http is None:
            with httpx.Client(timeout=30.0, follow_redirects=False) as http:
                return self._download_report_document(http, report_document_id)
        return self._download_report_document(self.http, report_document_id)

    def _download_report_document(self, http: httpx.Client, report_document_id: str) -> str:
        self.config.validate()
        token = self._access_token(http)
        metadata = http.get(
            f"{self.config.endpoint}/reports/2021-06-30/documents/{report_document_id}",
            headers={"x-amz-access-token": token},
        )
        self._raise_for_status(metadata, "get a report document")
        payload = metadata.json()
        url = payload.get("url")
        if not isinstance(url, str) or not self._is_allowed_download_url(url):
            raise AmazonSpApiError("Amazon returned an invalid report download URL.")
        response = http.get(url)
        self._raise_for_status(response, "download a report document")
        body = response.content
        if len(body) > MAX_REPORT_BYTES:
            raise AmazonSpApiError("Amazon report exceeds the 25 MB safety limit.")
        if payload.get("compressionAlgorithm") == "GZIP":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError) as exc:
                raise AmazonSpApiError("Amazon returned an invalid compressed report.") from exc
            if len(body) > MAX_REPORT_BYTES:
                raise AmazonSpApiError("Decompressed Amazon report exceeds the 25 MB safety limit.")
        return body.decode("utf-8-sig", errors="replace")

    @staticmethod
    def _is_allowed_download_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host.endswith(".amazonaws.com") or host.endswith(".cloudfront.net")
        )

    def _list_settlement_reports(self, http: httpx.Client) -> list[dict[str, object]]:
        self.config.validate()
        token = self._access_token(http)
        response = http.get(
            f"{self.config.endpoint}/reports/2021-06-30/reports",
            headers={"x-amz-access-token": token},
            params={
                "reportTypes": SETTLEMENT_REPORT_TYPE,
                "processingStatuses": "DONE",
                "pageSize": 10,
            },
        )
        self._raise_for_status(response, "list settlement reports")
        reports = response.json().get("reports", [])
        return reports if isinstance(reports, list) else []

    def _access_token(self, http: httpx.Client) -> str:
        response = http.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.config.refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            },
        )
        self._raise_for_status(response, "obtain an access token")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise AmazonSpApiError("Amazon did not return an access token.")
        return token

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.is_error:
            raise AmazonSpApiError(f"Amazon could not {action} (HTTP {response.status_code}).")
