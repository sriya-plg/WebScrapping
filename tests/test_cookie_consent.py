import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.base_scraper import GenericScraper
from app.core.carrier_config import load_carrier_config


class FakeLocator:
    def __init__(self, visible):
        self.visible = visible
        self.click_count = 0
        self.value = ""
        self.enabled = True

    @property
    def first(self):
        return self

    def is_visible(self, timeout):
        return self.visible

    def wait_for(self, state, timeout):
        if state == "hidden" and self.visible:
            self.visible = False

    def fill(self, value):
        self.value = value

    def input_value(self):
        return self.value

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.click_count += 1


class FakeFrame:
    url = "https://carrier.example/"

    def __init__(self, locator):
        self._locator = locator

    def locator(self, selector):
        return self._locator


class FakePage:
    def __init__(self, frames):
        self.frames = frames

    def locator(self, selector):
        return self.frames[0].locator(selector)

    def wait_for_timeout(self, timeout):
        return None


class CookieConsentTests(unittest.TestCase):
    def make_scraper(self, selector):
        scraper = GenericScraper.__new__(GenericScraper)
        scraper.config = SimpleNamespace(
            name="test-carrier",
            cookie_consent=SimpleNamespace(accept_selector=selector),
        )
        scraper.logger = Mock()
        return scraper

    def test_accepts_visible_cookie_banner(self):
        locator = FakeLocator(visible=True)
        scraper = self.make_scraper('[data-tid="banner-accept"]')

        scraper._handle_cookie_consent(FakePage([FakeFrame(locator)]))

        self.assertEqual(locator.click_count, 1)
        scraper.logger.info.assert_any_call(
            "cookie_consent_detected",
            extra={"carrier": "test-carrier", "frame_url": "https://carrier.example/"},
        )
        scraper.logger.info.assert_any_call(
            "cookie_consent_accepted",
            extra={"carrier": "test-carrier", "frame_url": "https://carrier.example/"},
        )

    def test_ignores_missing_cookie_banner(self):
        locator = FakeLocator(visible=False)
        scraper = self.make_scraper('[data-tid="banner-accept"]')

        scraper._handle_cookie_consent(FakePage([FakeFrame(locator)]))

        self.assertEqual(locator.click_count, 0)
        scraper.logger.debug.assert_any_call(
            "cookie_consent_not_present",
            extra={"carrier": "test-carrier", "selector": '[data-tid="banner-accept"]'},
        )

    def test_disabled_cookie_consent_is_a_no_op(self):
        locator = FakeLocator(visible=True)
        scraper = self.make_scraper("")

        scraper._handle_cookie_consent(FakePage([FakeFrame(locator)]))

        self.assertEqual(locator.click_count, 0)
        scraper.logger.debug.assert_not_called()

    def test_reference_value_is_verified_after_fill(self):
        locator = FakeLocator(visible=True)
        scraper = self.make_scraper("")
        page = FakePage([FakeFrame(locator)])

        scraper._fill_reference(page, "#criteria", "1848665720")

        self.assertEqual(locator.input_value(), "1848665720")
        scraper.logger.debug.assert_any_call(
            "reference_field_verified",
            extra={
                "carrier": "test-carrier",
                "reference": "1848665720",
                "selector": "#criteria",
            },
        )

    def test_submit_requires_enabled_button_and_clicks_it(self):
        locator = FakeLocator(visible=True)
        scraper = self.make_scraper("")
        page = FakePage([FakeFrame(locator)])
        locator.fill("1848665720")

        scraper._click_submit(
            page,
            'button:has-text("SEARCH")',
            "#criteria",
            "1848665720",
        )

        self.assertEqual(locator.click_count, 1)
        scraper.logger.debug.assert_any_call(
            "reference_before_submit_verified",
            extra={
                "carrier": "test-carrier",
                "reference": "1848665720",
                "selector": "#criteria",
            },
        )
        scraper.logger.debug.assert_any_call(
            "submit_click_completed",
            extra={"carrier": "test-carrier", "selector": 'button:has-text("SEARCH")'},
        )

    def test_estes_cookie_selector_is_loaded_from_yaml(self):
        config = load_carrier_config("estes")

        self.assertEqual(config.cookie_consent.accept_selector, '[data-tid="banner-accept"]')


if __name__ == "__main__":
    unittest.main()
