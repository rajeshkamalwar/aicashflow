import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from ai_cashflow.amazon_sp_api import AmazonSpApiClient, AmazonSpApiConfig


class AmazonSpApiClientTests(unittest.TestCase):
    def test_transaction_page_uses_immutable_before_and_reports_rate_limit(self):
        requests = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(
                200,
                headers={"x-amzn-RateLimit-Limit": "0.5"},
                json={"payload": {"transactions": [{"transactionId": "T1"}], "nextToken": "next"}},
            )
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            page = AmazonSpApiClient(
                AmazonSpApiConfig("client", "secret", "refresh"), http=http
            ).list_transactions_page("CA", "DEFERRED", start, end)

        self.assertEqual(requests[1].url.params["postedAfter"], "2026-08-01T00:00:00Z")
        self.assertEqual(requests[1].url.params["postedBefore"], "2026-08-02T00:00:00Z")
        self.assertEqual(page["next_token"], "next")
        self.assertEqual(page["rate_limit"], "0.5")

    def test_transaction_page_rejects_invalid_or_unsupported_windows(self):
        client = AmazonSpApiClient(AmazonSpApiConfig("client", "secret", "refresh"))
        start = datetime(2026, 8, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "positive"):
            client.list_transactions_page("CA", "DEFERRED", start, start)
        with self.assertRaisesRegex(ValueError, "180"):
            client.list_transactions_page(
                "CA", "DEFERRED", start - timedelta(days=181), start
            )

    def test_transaction_page_retries_retryable_failures_with_retry_after(self):
        attempts = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "token"})
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "0.001"})
            return httpx.Response(200, json={"payload": {"transactions": []}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            page = AmazonSpApiClient(
                AmazonSpApiConfig("client", "secret", "refresh"), http=http
            ).list_transactions_page(
                "CA", "RELEASED",
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                datetime(2026, 8, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(page["throttled_attempts"], 1)

    def test_transaction_pages_follow_the_returned_request_rate(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(
                200,
                headers={"x-amzn-RateLimit-Limit": "0.5"},
                json={"payload": {"transactions": []}},
            )
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = AmazonSpApiClient(
                AmazonSpApiConfig("client", "secret", "refresh"), http=http
            )
            with patch("ai_cashflow.amazon_sp_api.monotonic", return_value=0), patch(
                "ai_cashflow.amazon_sp_api.sleep"
            ) as delay:
                for _ in range(2):
                    client.list_transactions_page(
                        "CA", "RELEASED",
                        datetime(2026, 8, 1, tzinfo=timezone.utc),
                        datetime(2026, 8, 2, tzinfo=timezone.utc),
                    )

        delay.assert_called_once_with(2.0)

    def test_lists_transaction_status_for_persisted_sync(self):
        requests = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(200, json={"payload": {"transactions": [{"transactionId": "T1"}]}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            rows = AmazonSpApiClient(AmazonSpApiConfig("client", "secret", "refresh"), http=http).list_transactions_by_status(
                "A2EUQ1WTGCTBG2", "DEFERRED", datetime(2026, 2, 6, tzinfo=timezone.utc)
            )
        self.assertEqual(rows, [{"transactionId": "T1"}])
        self.assertEqual(requests[1].url.params["transactionStatus"], "DEFERRED")

    def test_transaction_status_retries_a_throttled_page(self):
        attempts = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "token"})
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"payload": {"transactions": []}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            AmazonSpApiClient(AmazonSpApiConfig("client", "secret", "refresh"), http=http).list_transactions_by_status(
                "A2EUQ1WTGCTBG2", "RELEASED", datetime(2026, 2, 6, tzinfo=timezone.utc)
            )
        self.assertEqual(attempts, 2)

    @staticmethod
    def _client_for_financial_groups(groups):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.path == "/finances/v0/financialEventGroups":
                return httpx.Response(200, json={"payload": {
                    "FinancialEventGroupList": groups,
                }})
            if request.url.path == "/finances/2024-06-19/transactions":
                return httpx.Response(200, json={"payload": {"transactions": []}})
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        return AmazonSpApiClient(
            AmazonSpApiConfig("client", "secret", "refresh"), http=http
        ), http

    @staticmethod
    def _group(group_id, status, amount, *, currency="CAD", **fields):
        return {
            "FinancialEventGroupId": group_id,
            "ProcessingStatus": status,
            "OriginalTotal": {
                "CurrencyCode": currency,
                "CurrencyAmount": amount,
            },
            "BeginningBalance": {
                "CurrencyCode": currency,
                "CurrencyAmount": "0",
            },
            **fields,
        }

    def test_lists_released_transactions_for_a_payment_group_with_pagination(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.params.get("nextToken") == "page-2":
                return httpx.Response(200, json={"payload": {
                    "transactions": [
                        {"transactionId": "T1"},
                        {"transactionId": "T2"},
                    ],
                }})
            return httpx.Response(200, json={"payload": {
                "transactions": [{"transactionId": "T1"}],
                "nextToken": "page-2",
            }})

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            rows = AmazonSpApiClient(config, http=http).list_payment_transactions(
                "ATVPDKIKX0DER",
                "GROUP-1",
                posted_after=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )

        self.assertEqual([row["transactionId"] for row in rows], ["T1", "T2"])
        first = requests[1].url.params
        self.assertEqual(first["transactionStatus"], "RELEASED")
        self.assertEqual(first["relatedIdentifierName"], "FINANCIAL_EVENT_GROUP_ID")
        self.assertEqual(first["relatedIdentifierValue"], "GROUP-1")
        self.assertEqual(requests[2].url.params["nextToken"], "page-2")

    def test_lists_financial_event_groups_for_a_window(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.params.get("NextToken") == "group-page-2":
                return httpx.Response(200, json={"payload": {
                    "FinancialEventGroupList": [{
                        "FinancialEventGroupId": "GROUP-2",
                        "ProcessingStatus": "Closed",
                    }],
                }})
            return httpx.Response(200, json={"payload": {
                "FinancialEventGroupList": [{
                    "FinancialEventGroupId": "GROUP-1",
                    "ProcessingStatus": "Closed",
                }],
                "NextToken": "group-page-2",
            }})

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            rows = AmazonSpApiClient(config, http=http).list_financial_event_groups(
                datetime(2026, 4, 1, tzinfo=timezone.utc)
            )

        self.assertEqual(
            [row["FinancialEventGroupId"] for row in rows],
            ["GROUP-1", "GROUP-2"],
        )
        self.assertIn(
            "2026-04-01",
            requests[1].url.params["FinancialEventGroupStartedAfter"],
        )
        self.assertEqual(requests[2].url.params["NextToken"], "group-page-2")

    def test_probes_finance_transactions(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            return httpx.Response(200, json={"payload": {"transactions": [{
                "marketplaceDetails": {
                    "marketplaceId": "A2EUQ1WTGCTBG2",
                    "marketplaceName": "Amazon.ca",
                },
            }]}})

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            rows = AmazonSpApiClient(config, http=http).probe_payments_access()

        self.assertEqual(rows[0]["marketplaceDetails"]["marketplaceName"], "Amazon.ca")
        self.assertEqual(requests[1].url.path, "/finances/2024-06-19/transactions")
        self.assertNotIn("marketplaceId", requests[1].url.params)

    def test_downloads_a_completed_report_document(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.host == "sellingpartnerapi-na.amazon.com":
                return httpx.Response(200, json={"url": "https://reports.amazonaws.com/report.txt"})
            return httpx.Response(200, content=b"settlement-id\tsettlement-start-date\nS1\t2026-01-01\n")

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            body = AmazonSpApiClient(config, http=http).download_report_document("D1")

        self.assertIn("settlement-id", body)

    def test_lists_completed_v2_settlement_reports(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            return httpx.Response(200, json={"reports": [{"reportId": "R1"}]})

        config = AmazonSpApiConfig("client", "secret", "refresh", "https://sellingpartnerapi-na.amazon.com")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            reports = AmazonSpApiClient(config, http=http).list_settlement_reports()

        self.assertEqual(reports, [{"reportId": "R1"}])
        self.assertEqual(requests[1].headers["x-amz-access-token"], "access-token")
        self.assertEqual(requests[1].url.params["reportTypes"], "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2")

    def test_refuses_requests_when_credentials_are_incomplete(self):
        with self.assertRaisesRegex(ValueError, "refresh token"):
            AmazonSpApiClient(AmazonSpApiConfig("client", "secret", "")).list_settlement_reports()

    def test_calculates_current_statement_position_like_seller_central(self):
        requests = []
        open_transfer_date = "2026-07-20T00:00:00Z"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            if request.url.path == "/finances/v0/financialEventGroups":
                return httpx.Response(200, json={"payload": {
                    "FinancialEventGroupList": [
                        {
                            "FinancialEventGroupId": "OPEN-1",
                            "ProcessingStatus": "Open",
                            "FinancialEventGroupStart": "2026-07-06T07:23:47Z",
                            "FundTransferDate": open_transfer_date,
                            "OriginalTotal": {
                                "CurrencyCode": "AUD",
                                "CurrencyAmount": 18720.03,
                            },
                            "BeginningBalance": {
                                "CurrencyCode": "AUD",
                                "CurrencyAmount": 32.57,
                            },
                        },
                        {
                            "FinancialEventGroupId": "BRL-OPEN-NEWER",
                            "ProcessingStatus": "Open",
                            "FinancialEventGroupStart": "2026-07-16T07:23:47Z",
                            "OriginalTotal": {
                                "CurrencyCode": "BRL",
                                "CurrencyAmount": 999.99,
                            },
                            "BeginningBalance": {
                                "CurrencyCode": "BRL",
                                "CurrencyAmount": 0,
                            },
                        },
                        {
                            "FinancialEventGroupId": "CLOSED-1",
                            "ProcessingStatus": "Closed",
                            "FinancialEventGroupStart": "2026-06-22T07:00:00Z",
                            "FundTransferDate": "2026-07-06T00:00:00Z",
                            "FundTransferStatus": "Succeeded",
                            "OriginalTotal": {
                                "CurrencyCode": "AUD",
                                "CurrencyAmount": 23535.57,
                            },
                        },
                        {
                            "FinancialEventGroupId": "BRL-CLOSED-NEWER",
                            "ProcessingStatus": "Closed",
                            "FinancialEventGroupStart": "2026-07-10T00:00:00Z",
                            "FundTransferDate": "2026-07-15T00:00:00Z",
                            "FundTransferStatus": "Succeeded",
                            "OriginalTotal": {
                                "CurrencyCode": "BRL",
                                "CurrencyAmount": 888.88,
                            },
                        },
                    ]
                }})
            if request.url.path == "/finances/2024-06-19/transactions":
                if request.url.params.get("transactionStatus") == "DEFERRED":
                    return httpx.Response(200, json={"payload": {"transactions": [{
                        "transactionId": "deferred-aggregate",
                        "transactionStatus": "DEFERRED",
                        "transactionType": "Shipment",
                        "totalAmount": {
                            "currencyCode": "AUD",
                            "currencyAmount": 32154.86,
                        },
                    }]}})
                return httpx.Response(200, json={"payload": {"transactions": [
                    {
                        "transactionId": "reserve-debit",
                        "transactionStatus": "RELEASED",
                        "transactionType": "Adjustment",
                        "description": "Reserve",
                        "totalAmount": {
                            "currencyCode": "AUD",
                            "currencyAmount": -32.57,
                        },
                    },
                    {
                        "transactionId": "reserve-credit",
                        "transactionStatus": "RELEASED",
                        "transactionType": "Adjustment",
                        "description": "Reserve",
                        "totalAmount": {
                            "currencyCode": "AUD",
                            "currencyAmount": 32.57,
                        },
                    },
                ]}})
            return httpx.Response(404)

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            position = AmazonSpApiClient(config, http=http).get_financial_position(
                "A39IBJ37TRP1C6",
                Decimal("0.7008311942"),
                expected_currency="AUD",
                now=datetime(2026, 7, 17, 12, 50, tzinfo=timezone.utc),
            )

        self.assertEqual(position["source_currency"], "AUD")
        self.assertEqual(position["currency"], "USD")
        self.assertEqual(position["source_amounts"]["standard_balance"], "18720.03")
        self.assertIsNone(position["source_amounts"]["deferred_balance"])
        self.assertIsNone(position["source_amounts"]["total_balance"])
        self.assertIsNone(position["source_amounts"]["funds_available"])
        self.assertEqual(position["source_amounts"]["recent_payout"], "23535.57")
        self.assertEqual(position["amounts"]["standard_balance"], "13119.58")
        self.assertIsNone(position["amounts"]["deferred_balance"])
        self.assertIsNone(position["amounts"]["total_balance"])
        self.assertIsNone(position["amounts"]["funds_available"])
        self.assertEqual(position["amounts"]["recent_payout"], "16494.46")
        self.assertIsNone(position["amounts"]["reserve_balance"])
        self.assertEqual(position["funds_available_status"], "unavailable")
        self.assertEqual(position["recent_payout_date"], "2026-07-06T00:00:00Z")
        self.assertEqual(position["expected_payout_forecast"], {
            "status": "unavailable",
            "availability_reason": "Amazon SP-API has not supplied an authoritative upcoming payout amount and transfer date.",
            "scheduled_date": None,
            "source_currency": "AUD",
            "currency": "USD",
            "source_amounts": {
                "this_week": None,
                "next_week": None,
                "all_upcoming": None,
                "unscheduled": None,
            },
            "amounts": {
                "this_week": None,
                "next_week": None,
                "all_upcoming": None,
                "unscheduled": None,
            },
        })
        values = position["financial_values"]
        open_balance = next(row for row in values if row["type"] == "OPEN_BALANCE")
        self.assertEqual(open_balance["amount"], "18720.03")
        self.assertEqual(open_balance["sourceField"], "OriginalTotal")
        self.assertEqual(open_balance["processingStatus"], "Open")
        self.assertEqual(open_balance["authoritativeFor"], "AMAZON_REPORTED_OPEN_BALANCE")
        self.assertTrue(open_balance["isAuthoritative"])
        self.assertIsNone(next(row for row in values if row["type"] == "CURRENT_RESERVE")["amount"])
        self.assertIsNone(next(row for row in values if row["type"] == "RESERVE_ADJUSTED_FUNDS")["amount"])
        self.assertIsNone(next(row for row in values if row["type"] == "UPCOMING_PAYOUT")["amount"])
        completed = next(row for row in values if row["type"] == "COMPLETED_PAYOUT")
        self.assertEqual(completed["amount"], "23535.57")
        self.assertEqual(completed["processingStatus"], "Closed")
        self.assertFalse(any(
            request.url.path == "/finances/2024-06-19/transactions"
            for request in requests
        ))
        self.assertFalse(any(
            request.url.path == "/finances/2024-06-19/transactions"
            and not request.url.params.get("transactionStatus")
            for request in requests
        ))

    def test_aggregates_unique_open_balances_across_currencies(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.amazon.com":
                return httpx.Response(200, json={"access_token": "access-token"})
            return httpx.Response(200, json={"payload": {
                "FinancialEventGroupList": [
                    {
                        "FinancialEventGroupId": "CAD-OPEN",
                        "ProcessingStatus": "Open",
                        "FinancialEventGroupStart": "2026-08-01T00:00:00Z",
                        "FundTransferDate": "2026-08-05T00:00:00Z",
                        "OriginalTotal": {"CurrencyCode": "CAD", "CurrencyAmount": "88495.71"},
                    },
                    {
                        "FinancialEventGroupId": "CAD-CLOSED-20260731",
                        "ProcessingStatus": "Closed",
                        "FinancialEventGroupStart": "2026-07-20T00:00:00Z",
                        "FundTransferDate": "2026-07-31T00:00:00Z",
                        "FundTransferStatus": "Succeeded",
                        "OriginalTotal": {"CurrencyCode": "CAD", "CurrencyAmount": "123025.62"},
                    },
                    {
                        "FinancialEventGroupId": "MXN-OPEN",
                        "ProcessingStatus": "Open",
                        "FinancialEventGroupStart": "2026-08-02T00:00:00Z",
                        "FundTransferDate": "2026-08-12T00:00:00Z",
                        "OriginalTotal": {"CurrencyCode": "MXN", "CurrencyAmount": "1000.00"},
                    },
                    {
                        "FinancialEventGroupId": "CAD-OPEN",
                        "ProcessingStatus": "Open",
                        "OriginalTotal": {"CurrencyCode": "CAD", "CurrencyAmount": "88495.71"},
                    },
                ]
            }})

        config = AmazonSpApiConfig("client", "secret", "refresh")
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            position = AmazonSpApiClient(config, http=http).get_current_balances(
                {"CAD": Decimal("0.33333"), "MXN": Decimal("0.003333")},
                now=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )

        self.assertEqual(position["mode"], "amazon_open_balances")
        self.assertEqual(position["group_count"], 2)
        self.assertEqual(position["totals_by_currency"], {"CAD": "88495.71", "MXN": "1000.00"})
        self.assertEqual(position["amounts"]["current_balance"], "29501.61")
        self.assertEqual(position["expected_payout_forecasts"], [])
        self.assertEqual(position["recent_completed_payouts"]["CAD"]["amount"], "123025.62")
        self.assertEqual(position["recent_completed_payouts"]["CAD"]["financial_event_group_id"], "CAD-…0731")
        open_values = [row for row in position["financial_values"] if row["type"] == "OPEN_BALANCE"]
        self.assertEqual(
            [(row["currency"], row["amount"]) for row in open_values],
            [("CAD", "88495.71"), ("MXN", "1000.00")],
        )
        self.assertFalse(any(
            row["type"] == "UPCOMING_PAYOUT" and row["amount"] is not None
            for row in position["financial_values"]
        ))
        self.assertEqual(
            sum(Decimal(row["usd_amount"]) for row in position["balances"]),
            Decimal(position["amounts"]["current_balance"]),
        )

    def test_canonical_selection_is_response_order_independent(self):
        observations = [
            self._group(
                "G1", "Open", "10.00",
                FinancialEventGroupStart="2026-08-01T00:00:00Z",
            ),
            self._group(
                "G1", "Closed", "12.00",
                FinancialEventGroupEnd="2026-08-03T00:00:00Z",
            ),
        ]
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        forward, forward_diagnostics = AmazonSpApiClient._normalize_financial_event_groups(
            observations, now
        )
        reverse, reverse_diagnostics = AmazonSpApiClient._normalize_financial_event_groups(
            list(reversed(observations)), now
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(forward_diagnostics, reverse_diagnostics)

    def test_open_followed_by_closed_selects_closed(self):
        observations = [
            self._group("G1", "Open", "10", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("G1", "Closed", "10", FinancialEventGroupEnd="2026-08-02T00:00:00Z"),
        ]

        canonical, _ = AmazonSpApiClient._normalize_financial_event_groups(
            observations, datetime(2026, 8, 4, tzinfo=timezone.utc)
        )

        self.assertEqual(canonical[0]["ProcessingStatus"], "Closed")

    def test_closed_followed_by_newer_open_selects_open(self):
        observations = [
            self._group("G1", "Closed", "8", FundTransferDate="2026-08-02T00:00:00Z"),
            self._group("G1", "Open", "9", FinancialEventGroupStart="2026-08-03T00:00:00Z"),
        ]

        canonical, _ = AmazonSpApiClient._normalize_financial_event_groups(
            observations, datetime(2026, 8, 4, tzinfo=timezone.utc)
        )

        self.assertEqual(canonical[0]["ProcessingStatus"], "Open")
        self.assertEqual(canonical[0]["OriginalTotal"]["CurrencyAmount"], "9")

    def test_revised_total_uses_newer_observation(self):
        observations = [
            self._group("G1", "Open", "10.001", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("G1", "Open", "10.009", FinancialEventGroupStart="2026-08-02T00:00:00Z"),
        ]
        client, http = self._client_for_financial_groups(observations)
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["totals_by_currency"], {"CAD": "10.01"})

    def test_revised_transfer_status_uses_stable_progress_tie_breaker(self):
        observations = [
            self._group(
                "G1", "Closed", "10", FundTransferDate="2026-08-03T00:00:00Z",
                FundTransferStatus="Pending",
            ),
            self._group(
                "G1", "Closed", "10", FundTransferDate="2026-08-03T00:00:00Z",
                FundTransferStatus="Succeeded",
            ),
        ]

        canonical, _ = AmazonSpApiClient._normalize_financial_event_groups(
            list(reversed(observations)), datetime(2026, 8, 4, tzinfo=timezone.utc)
        )

        self.assertEqual(canonical[0]["FundTransferStatus"], "Succeeded")

    def test_identical_duplicate_observations_collapse_to_one_group(self):
        observation = self._group(
            "G1", "Open", "4.20", FinancialEventGroupStart="2026-08-01T00:00:00Z"
        )

        canonical, diagnostics = AmazonSpApiClient._normalize_financial_event_groups(
            [observation, dict(observation)],
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        self.assertEqual(len(canonical), 1)
        self.assertEqual(diagnostics[0]["observationCount"], 2)

    def test_pagination_duplicates_do_not_double_count(self):
        observation = self._group(
            "G1", "Open", "7.25", FinancialEventGroupStart="2026-08-01T00:00:00Z"
        )
        client, http = self._client_for_financial_groups([observation, dict(observation)])
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["group_count"], 1)
        self.assertEqual(position["totals_by_currency"]["CAD"], "7.25")

    def test_missing_timestamps_use_deterministic_terminal_state_tie_breaker(self):
        observations = [
            self._group("G1", "Open", "9.99"),
            self._group("G1", "Closed", "9.99"),
        ]
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)

        forward, _ = AmazonSpApiClient._normalize_financial_event_groups(observations, now)
        reverse, _ = AmazonSpApiClient._normalize_financial_event_groups(
            list(reversed(observations)), now
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0]["ProcessingStatus"], "Closed")

    def test_retrieval_timestamp_precedes_status_when_event_times_tie(self):
        observations = [
            self._group(
                "G1", "Closed", "10",
                FinancialEventGroupStart="2026-08-01T00:00:00Z",
                retrievalTimestamp="2026-08-03T00:00:00Z",
            ),
            self._group(
                "G1", "Open", "11",
                FinancialEventGroupStart="2026-08-01T00:00:00Z",
                retrievalTimestamp="2026-08-04T00:00:00Z",
            ),
        ]

        canonical, _ = AmazonSpApiClient._normalize_financial_event_groups(
            observations, datetime(2026, 8, 5, tzinfo=timezone.utc)
        )

        self.assertEqual(canonical[0]["ProcessingStatus"], "Open")
        self.assertEqual(canonical[0]["OriginalTotal"]["CurrencyAmount"], "11")

    def test_multiple_open_groups_in_one_currency_are_summed(self):
        groups = [
            self._group("CAD-1", "Open", "10.10", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("CAD-2", "Open", "20.20", FinancialEventGroupStart="2026-08-02T00:00:00Z"),
        ]
        client, http = self._client_for_financial_groups(groups)
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["totals_by_currency"], {"CAD": "30.30"})
        self.assertEqual(position["openSettlementGroupCount"], 2)

    def test_selected_currency_settlement_count_excludes_other_currencies(self):
        groups = [
            self._group("CAD-1", "Open", "10", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("CAD-2", "Open", "20", FinancialEventGroupStart="2026-08-02T00:00:00Z"),
            self._group("USD-1", "Open", "30", currency="USD", FinancialEventGroupStart="2026-08-03T00:00:00Z"),
        ]
        client, http = self._client_for_financial_groups(groups)
        try:
            position = client.get_financial_position(
                "A2EUQ1WTGCTBG2", Decimal("1"), expected_currency="CAD",
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["openSettlementGroupCount"], 2)
        self.assertEqual(position["group_count"], 2)

    def test_all_currency_cad_equals_selected_cad(self):
        groups = [
            self._group("CAD-1", "Open", "10.10", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("CAD-2", "Open", "20.20", FinancialEventGroupStart="2026-08-02T00:00:00Z"),
            self._group("USD-1", "Open", "99", currency="USD", FinancialEventGroupStart="2026-08-03T00:00:00Z"),
        ]
        all_client, all_http = self._client_for_financial_groups(groups)
        cad_client, cad_http = self._client_for_financial_groups(groups)
        try:
            all_position = all_client.get_current_balances(
                {"CAD": Decimal("1"), "USD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
            cad_position = cad_client.get_financial_position(
                "A2EUQ1WTGCTBG2", Decimal("1"), expected_currency="CAD",
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            all_http.close()
            cad_http.close()

        self.assertEqual(
            all_position["totals_by_currency"]["CAD"],
            cad_position["source_amounts"]["standard_balance"],
        )

    def test_all_currency_subtotals_equal_each_selected_currency(self):
        groups = [
            self._group(f"{currency}-1", "Open", amount, currency=currency, FinancialEventGroupStart="2026-08-01T00:00:00Z")
            for currency, amount in (("CAD", "10.10"), ("BRL", "20.20"), ("MXN", "30.30"), ("USD", "40.40"))
        ]
        rates = {currency: Decimal("1") for currency in ("CAD", "BRL", "MXN", "USD")}
        all_client, all_http = self._client_for_financial_groups(groups)
        try:
            all_position = all_client.get_current_balances(
                rates,
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            all_http.close()

        for currency in rates:
            client, http = self._client_for_financial_groups(groups)
            try:
                selected = client.get_financial_position(
                    "A2EUQ1WTGCTBG2", Decimal("1"), expected_currency=currency,
                    now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                )
            finally:
                http.close()
            with self.subTest(currency=currency):
                self.assertEqual(
                    all_position["totals_by_currency"][currency],
                    selected["source_amounts"]["standard_balance"],
                )

    def test_decimal_precision_is_preserved_until_money_formatting(self):
        groups = [
            self._group("CAD-1", "Open", "0.105", FinancialEventGroupStart="2026-08-01T00:00:00Z"),
            self._group("CAD-2", "Open", "0.105", FinancialEventGroupStart="2026-08-02T00:00:00Z"),
        ]
        client, http = self._client_for_financial_groups(groups)
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["totals_by_currency"]["CAD"], "0.21")

    def test_empty_financial_event_group_state_is_valid(self):
        client, http = self._client_for_financial_groups([])
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        self.assertEqual(position["status"], "unavailable")
        self.assertEqual(position["balances"], [])
        self.assertEqual(position["totals_by_currency"], {})
        self.assertEqual(position["openSettlementGroupCount"], 0)

    def test_reserve_and_payout_fields_remain_unavailable(self):
        group = self._group(
            "CAD-1", "Open", "50", FinancialEventGroupStart="2026-08-01T00:00:00Z"
        )
        client, http = self._client_for_financial_groups([group])
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        protected = {"CURRENT_RESERVE", "RESERVE_ADJUSTED_FUNDS", "UPCOMING_PAYOUT"}
        self.assertTrue(all(
            row["amount"] is None
            for row in position["financial_values"]
            if row["type"] in protected
        ))

    def test_open_original_total_never_becomes_upcoming_payout(self):
        group = self._group(
            "CAD-1", "Open", "50", FinancialEventGroupStart="2026-08-01T00:00:00Z"
        )
        client, http = self._client_for_financial_groups([group])
        try:
            position = client.get_current_balances(
                {"CAD": Decimal("1")},
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            )
        finally:
            http.close()

        open_balance = next(row for row in position["financial_values"] if row["type"] == "OPEN_BALANCE")
        upcoming = next(row for row in position["financial_values"] if row["type"] == "UPCOMING_PAYOUT")
        self.assertEqual(open_balance["amount"], "50.00")
        self.assertEqual(open_balance["sourceField"], "OriginalTotal")
        self.assertIsNone(upcoming["amount"])
        self.assertNotIn("financial_event_group_diagnostics", position)
