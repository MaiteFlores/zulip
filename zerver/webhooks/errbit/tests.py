# -*- coding: utf-8 -*-
from zerver.lib.test_classes import WebhookTestCase


class ErrBitHookTests(WebhookTestCase):
    STREAM_NAME = 'errbit'
    URL_TEMPLATE = u"/api/v1/external/errbit?stream={stream}&api_key={api_key}"
    FIXTURE_DIR_NAME = 'errbit'

    def test_errbit_error_message(self) -> None:
        expected_topic = u"ZulipIntegrationTest / ErrbitEnvName"
        expected_message = u"[IllegalStateException](https://errbit.example.com/apps/5e1ed1ff1a603f3916f4f0de/problems/5e1fe93e1a603f3916f4f0e3): \"Invalid state error\" occurred."
        self.send_and_test_stream_message('error_message', expected_topic, expected_message)
