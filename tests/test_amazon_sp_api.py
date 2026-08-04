import unittest
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from ai_cashflow.amazon_sp_api import AmazonSpApiClient, AmazonSpApiConfig


class AmazonSpApiClientTests(unittest.TestCase):
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
        self.assertEqual(position["source_amounts"]["deferred_balance"], "32154.86")
        self.assertEqual(position["source_amounts"]["total_balance"], "50874.89")
        self.assertIsNone(position["source_amounts"]["funds_available"])
        self.assertEqual(position["source_amounts"]["recent_payout"], "23535.57")
        self.assertEqual(position["amounts"]["standard_balance"], "13119.58")
        self.assertEqual(position["amounts"]["deferred_balance"], "22535.13")
        self.assertEqual(position["amounts"]["total_balance"], "35654.71")
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
        self.assertTrue(any(
            request.url.params.get("transactionStatus") == "DEFERRED"
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
