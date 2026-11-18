import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import aliexpress_export as exporter


def success(blocks):
    return {"ret": ["SUCCESS"], "data": {"data": blocks}}


def order_page(order_id, page, more):
    return success(
        {
            "order": {
                "tag": "pc_om_list_order",
                "fields": {
                    "orderId": order_id,
                    "orderDateText": "2026-10-27" if order_id == "a" else "2026-10-20",
                },
            },
            "body": {
                "tag": "pc_om_list_body",
                "fields": {"pageIndex": page, "hasMore": more},
            },
        }
    )


def detail(order_id):
    return success(
        {
            "info": {
                "tag": "detail_simple_order_info_component",
                "fields": {"tradeOrderId": order_id},
            },
        }
    )


class TokenRecoveryTest(unittest.TestCase):
    def test_resigns_with_rotated_cookie_and_retries_once(self):
        for code in ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED"):
            with self.subTest(code=code):
                client = exporter.AliExpressMtopClient(
                    [{"name": "_m_h5_tk", "value": "old_123", "domain": ".aliexpress.com"}]
                )
                queries = []

                def request(args, *, expect_json):
                    if not expect_json:
                        return {}
                    queries.append(parse_qs(urlsplit(args[-1]).query))
                    client.cookies[0]["value"] = "new_456"
                    return {"ret": [code]} if len(queries) == 1 else success({})

                try:
                    with patch.object(client, "_run_curl", side_effect=request):
                        result = client.fetch_order_detail("synthetic-1")
                    self.assertEqual(result["ret"], ["SUCCESS"])
                    self.assertEqual(len(queries), 2)
                    self.assertNotEqual(queries[0]["sign"], queries[1]["sign"])
                finally:
                    client.close()

    def test_token_retry_is_bounded_and_session_expiry_is_not_retried(self):
        for code, count in (("FAIL_SYS_TOKEN_EXOIRED", 2), ("FAIL_SYS_SESSION_EXPIRED", 1)):
            client = exporter.AliExpressMtopClient([])
            try:
                with (
                    patch.object(client, "_request_once", return_value={"ret": [code]}) as request,
                    patch.object(client, "bootstrap"),
                ):
                    result = client._request("test", {}, method="GET")
                self.assertEqual(request.call_count, count)
                with self.assertRaises(RuntimeError):
                    exporter.ensure_successful_mtop(result, "test")
            finally:
                client.close()


class ResumeTest(unittest.TestCase):
    def config(self, directory):
        return exporter.parse_args(
            [
                "--start-date",
                "2026-01-01",
                "--end-date",
                "2026-11-18",
                "--output-dir",
                str(directory),
                "--cookies-path",
                str(directory / "cookies.json"),
                "--max-pages",
                "3",
                "--download-pdfs",
            ]
        )

    def client(self):
        client = Mock()
        client.export_cookies.return_value = []
        client.fetch_order_list_page.return_value = order_page("a", 1, True)
        client.fetch_order_list_page_more.return_value = order_page("b", 2, False)
        client.fetch_order_detail.side_effect = detail
        client.fetch_invoice_info.return_value = success({"invoiceInfoDTOList": []})
        return client

    def test_interrupted_details_resume_and_replay_all_pages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            first = self.client()
            first.fetch_order_detail.side_effect = [detail("a"), RuntimeError("expired")]
            with patch.object(exporter, "AliExpressMtopClient", return_value=first):
                with self.assertRaisesRegex(RuntimeError, "expired"):
                    exporter.run_export(config)
            checkpoint_path = next(root.glob("checkpoint_*.json"))
            checkpoint = json.loads(checkpoint_path.read_text())
            self.assertEqual(checkpoint["details_done"], ["a"])
            self.assertEqual(checkpoint["invoices_done"], ["a"])
            self.assertEqual(len(checkpoint["orders"]), 2)
            self.assertTrue(list(root.glob("aliexpress_orders_*.csv")))
            first.close.assert_called_once()

            second = self.client()
            with patch.object(exporter, "AliExpressMtopClient", return_value=second):
                exporter.run_export(config)
            second.fetch_order_list_page_more.assert_called_once()
            second.fetch_order_detail.assert_called_once_with("b")
            second.fetch_invoice_info.assert_called_once_with("b")
            checkpoint = json.loads(checkpoint_path.read_text())
            self.assertEqual(checkpoint["details_done"], ["a", "b"])

    def test_invoice_failure_remains_pending_after_detail_is_saved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            first = self.client()
            first.fetch_invoice_info.side_effect = exporter.MtopError(
                "invoice", ["FAIL_SYS_SESSION_EXPIRED"]
            )
            with patch.object(exporter, "AliExpressMtopClient", return_value=first):
                with self.assertRaises(RuntimeError):
                    exporter.run_export(config)
            checkpoint = json.loads(next(root.glob("checkpoint_*.json")).read_text())
            self.assertEqual(checkpoint["details_done"], ["a"])
            self.assertEqual(checkpoint["invoices_done"], [])

    def test_pdf_retry_reuses_saved_details_and_prior_pdf(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            first = self.client()
            first.fetch_invoice_info.return_value = success(
                {
                    "invoiceInfoDTOList": [{"invoiceId": "one"}, {"invoiceId": "two"}],
                }
            )
            first.fetch_invoice_file.side_effect = [
                b"%PDF-synthetic",
                exporter.MtopError("invoice", ["FAIL_SYS_TOKEN_EXOIRED"]),
            ]
            with patch.object(exporter, "AliExpressMtopClient", return_value=first):
                with self.assertRaises(RuntimeError):
                    exporter.run_export(config)
            self.assertEqual(len(list((root / "pdf").glob("*.pdf"))), 1)
            second = self.client()
            second.fetch_invoice_file.return_value = b"%PDF-synthetic"
            with patch.object(exporter, "AliExpressMtopClient", return_value=second):
                exporter.run_export(config)
            self.assertEqual(second.fetch_invoice_file.call_count, 1)
            self.assertEqual(second.fetch_invoice_file.call_args.args[1]["invoiceId"], "two")
            second.fetch_order_detail.assert_called_once_with("b")

    def test_invalid_pdf_is_not_checkpointed_and_retry_downloads_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            client = self.client()
            client.fetch_invoice_info.return_value = success(
                {
                    "invoiceInfoDTOList": [{"invoiceId": "one"}],
                }
            )
            client.fetch_invoice_file.return_value = b"<html>login</html>"
            with patch.object(exporter, "AliExpressMtopClient", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "remain pending"):
                    exporter.run_export(config)
            self.assertEqual(list((root / "pdf").glob("*.pdf")), [])
            checkpoint = json.loads(next(root.glob("checkpoint_*.json")).read_text())
            self.assertEqual(checkpoint["orders"]["a"]["invoice_pdf_paths"], [])

    def test_unavailable_invoice_does_not_block_later_orders_and_is_retried(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            first = self.client()
            first.fetch_invoice_info.side_effect = [
                exporter.MtopError("invoice", ["BUSINESS_INVOICE_UNAVAILABLE"]),
                success({"invoiceInfoDTOList": []}),
            ]
            with patch.object(exporter, "AliExpressMtopClient", return_value=first):
                with self.assertRaisesRegex(RuntimeError, "1 invoice operation"):
                    exporter.run_export(config)
            saved = json.loads(next(root.glob("checkpoint_*.json")).read_text())
            self.assertEqual(saved["details_done"], ["a", "b"])
            self.assertEqual(saved["invoices_done"], ["b"])
            second = self.client()
            with patch.object(exporter, "AliExpressMtopClient", return_value=second):
                exporter.run_export(config)
            second.fetch_order_detail.assert_not_called()
            second.fetch_invoice_info.assert_called_once_with("a")

    def test_atomic_replace_failure_preserves_previous_checkpoint(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_bytes(b"old")
            with patch.object(exporter.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    exporter.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_rotated_cookies_are_saved_when_later_request_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            Path(config.cookies_path).write_text("[]")
            client = self.client()
            client.fetch_order_detail.side_effect = exporter.MtopError(
                "order.detail", ["FAIL_SYS_SESSION_EXPIRED"]
            )
            client.export_cookies.return_value = [
                {
                    "name": "_m_h5_tk",
                    "value": "synthetic_rotated",
                    "domain": ".aliexpress.com",
                    "path": "/",
                }
            ]
            with patch.object(exporter, "AliExpressMtopClient", return_value=client):
                with self.assertRaises(exporter.MtopError):
                    exporter.run_export(config)
            self.assertEqual(
                json.loads(Path(config.cookies_path).read_text())[0]["value"],
                "synthetic_rotated",
            )

    @unittest.skipIf(os.name == "nt", "POSIX locking regression")
    def test_concurrent_export_is_rejected_before_network(self):
        import fcntl

        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            pdf = root / "pdf"
            pdf.mkdir()
            with (root / ".export.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(exporter, "AliExpressMtopClient") as client:
                    with self.assertRaisesRegex(RuntimeError, "Another export"):
                        exporter.export_live(config, Path(config.cookies_path), pdf)
                client.assert_not_called()

    def test_mismatched_scope_fails_before_network_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            pdf = root / "pdf"
            pdf.mkdir()
            checkpoint = exporter.ExportCheckpoint(config, Path(config.cookies_path), pdf)
            checkpoint.save()
            config.lang = "different"
            with patch.object(exporter, "AliExpressMtopClient") as client:
                with self.assertRaisesRegex(RuntimeError, "settings differ"):
                    exporter.export_live(config, Path(config.cookies_path), pdf)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
