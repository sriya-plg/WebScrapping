"""
Loads a carrier's YAML config into a typed object the generic scraper
engine can consume. Adding a new carrier = adding a new YAML file here
that follows this shape -- no code changes required for the interaction
logic itself.
"""

import os
from dataclasses import dataclass, field

import yaml

from app.core.exceptions import CarrierConfigError

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "carriers")


@dataclass(frozen=True)
class LoginConfig:
    required: bool = False
    username_input: str = ""
    password_input: str = ""
    submit_button: str = ""


@dataclass(frozen=True)
class ReferenceTypeConfig:
    """Some sites require selecting which field to search by (BOL vs PRO
    vs PO) before/alongside entering the number, e.g. a dropdown. `options`
    maps our internal reference_type string ("bol", "pro", "po") to the
    literal value/label that site's dropdown expects."""
    selector: str = ""
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CookieConsentConfig:
    accept_selector: str = ""


@dataclass(frozen=True)
class CarrierConfig:
    name: str
    tracking_url: str
    api_url_substring: str
    wait_until: str
    bot_protected: bool
    reference_input_selector: str
    submit_button_selector: str
    supported_reference_types: list[str]
    login: LoginConfig
    reference_type_field: ReferenceTypeConfig
    cookie_consent: CookieConsentConfig
    api_url_template: str = ""


def load_carrier_config(carrier_name: str) -> CarrierConfig:
    path = os.path.join(CONFIG_DIR, f"{carrier_name}.yaml")
    if not os.path.exists(path):
        raise CarrierConfigError(f"No config found for carrier '{carrier_name}' at {path}")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    try:
        selectors = raw["selectors"]
        login_raw = raw.get("login", {}) or {}
        ref_type_raw = raw.get("reference_type_field", {}) or {}
        cookie_consent_raw = raw.get("cookie_consent", {}) or {}

        return CarrierConfig(
            name=raw["name"],
            tracking_url=raw["tracking_url"],
            api_url_substring=raw["api_url_substring"],
            wait_until=raw.get("wait_until", "domcontentloaded"),
            bot_protected=raw.get("bot_protected", False),
            reference_input_selector=selectors["reference_input"],
            submit_button_selector=selectors["submit_button"],
            supported_reference_types=raw.get("supported_reference_types", []),
            login=LoginConfig(
                required=login_raw.get("required", False),
                username_input=login_raw.get("username_input", ""),
                password_input=login_raw.get("password_input", ""),
                submit_button=login_raw.get("submit_button", ""),
            ),
            reference_type_field=ReferenceTypeConfig(
                selector=ref_type_raw.get("selector", ""),
                options=ref_type_raw.get("options", {}),
            ),
            cookie_consent=CookieConsentConfig(
                accept_selector=cookie_consent_raw.get("accept_selector", ""),
            ),
            api_url_template=raw.get("api_url_template", ""),
        )
    except KeyError as e:
        raise CarrierConfigError(f"Config for '{carrier_name}' missing required field: {e}")
