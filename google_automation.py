"""
Google One automation using Selenium.

Logs into a Gmail account, navigates to Google One, detects the
12-month free Gemini Pro offer, and returns the activation / payment link.
"""

import asyncio
import logging
import time
import re
from urllib.parse import urlparse
from typing import Optional, Callable, Awaitable
 
import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Driver factory ────────────────────────────────────────────────────────────

def _build_driver(profile: DeviceProfile) -> uc.Chrome:
    """Return a headless Chrome WebDriver configured for the device profile."""
    options = uc.ChromeOptions()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    # Standard arguments to improve stability and reduce detection
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={profile.user_agent}")

    # Arguments to hide automation flags
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument('--disable-component-update')

    # undetected-chromedriver handles driver management automatically
    driver = uc.Chrome(
        options=options,
        version_main=config.CHROME_MAJOR_VERSION,
        enable_cdp_events=True, # Needed for some advanced features
    )
    
    # Execute CDP command to remove automation flags from JavaScript
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


# ── Login helper ──────────────────────────────────────────────────────────────

def _wait_for(driver: uc.Chrome, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> object:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


async def _gmail_login(
    driver: uc.Chrome,
    email: str,
    password: str,
    request_2fa_callback: Callable[[], Awaitable[str]],
) -> bool:
    """
    Perform Gmail / Google account login.

    Returns True on apparent success, False on detectable failure.
    """
    try:
        driver.get(config.GMAIL_LOGIN_URL)

        # ── Email step ────────────────────────────────────────────────────────
        email_field = _wait_for(
            driver,
            By.CSS_SELECTOR,
            'input[name="identifier"], input[type="email"], input#identifierId',
        )
        email_field.clear()
        email_field.send_keys(email)
        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()

        # ── Password step ─────────────────────────────────────────────────────
        try:
            password_field = WebDriverWait(driver, 15).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[name="Passwd"], input[name="password"], input[type="password"]'))
            )
        except TimeoutException:
            logger.warning("Password field did not appear. Email may be invalid.")
            return False

        password_field.clear()
        password_field.send_keys(password)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()

        # ── 2FA / Challenge step (if it appears) ─────────────────────────────
        try:
            # Wait briefly to see if redirected to a challenge page or PIN input
            WebDriverWait(driver, 10).until(
                lambda d: "challenge" in d.current_url or d.find_elements(By.CSS_SELECTOR, '#securityKeyOtpInputId, #totpPin, #idvPin, input[type="tel"]')
            )
            if "challenge" in driver.current_url or driver.find_elements(By.CSS_SELECTOR, '#securityKeyOtpInputId, #totpPin, #idvPin, input[type="tel"]'):
                logger.info("2FA / verification challenge requested by Google (URL: %s).", driver.current_url)

                # Check for challenge options on any challenge page (selection, pwd, etc.)
                if "challenge" in driver.current_url:
                    ebhgs = driver.find_elements(By.CSS_SELECTOR, 'div[jsname="EBHGs"], [data-challengetype]')
                    if not ebhgs:
                        # Try clicking "Tentar de outro jeito" / "Try another way" if options list isn't directly shown
                        other_ways = driver.find_elements(By.CSS_SELECTOR, 'button, div[role="link"], a, li')
                        for ow in other_ways:
                            txt = ow.text.lower()
                            if "outro jeito" in txt or "another way" in txt:
                                logger.info("Clicking 'Try another way' link: %s", ow.text[:60])
                                driver.execute_script("arguments[0].click();", ow)
                                time.sleep(3)
                                ebhgs = driver.find_elements(By.CSS_SELECTOR, 'div[jsname="EBHGs"], [data-challengetype]')
                                break

                    # Priority-based selection: find the best challenge type
                    best_type = None
                    for el in ebhgs:
                        ctype = el.get_attribute("data-challengetype") or ""
                        if ctype == "8":
                            best_type = "8"
                            best_el = el
                            break
                        elif ctype == "9" and best_type is None:
                            best_type = "9"
                            best_el = el
                        elif ctype == "5" and best_type is None:
                            best_type = "5"
                            best_el = el
                        elif ctype == "39" and best_type is None:
                            best_type = "39"
                            best_el = el
                        elif ctype == "37" and best_type is None:
                            best_type = "37"
                            best_el = el

                    if best_type:
                        logger.info("Clicking challenge option type=%s: %s", best_type, best_el.text[:60])
                        driver.execute_script("arguments[0].click();", best_el)
                        time.sleep(4)

                # Look for PIN / Security Code input field
                try:
                    pin_field = WebDriverWait(driver, 15).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, '#securityKeyOtpInputId, #totpPin, #idvPin, input[name="Pin"], input[name="pin"], input[type="tel"], input[name="code"]'))
                    )
                    logger.info("PIN / Security code input field ready. Prompting user...")
                    otp_code = await request_2fa_callback()
                    if not otp_code:
                        logger.warning("User did not provide a 2FA / Security code.")
                        return False
                    pin_field.clear()
                    pin_field.send_keys(otp_code)
                    time.sleep(1)
                    
                    next_btns = driver.find_elements(By.CSS_SELECTOR, '#totpNext, #idvPreregisteredPhoneNext, #idvanyphoneNext, [id$="Next"], button[type="submit"]')
                    if next_btns:
                        driver.execute_script("arguments[0].click();", next_btns[0])
                    else:
                        pin_field.send_keys('\n')
                except TimeoutException:
                    logger.info("No PIN input field located; waiting for user prompt/approval on device.")

        except TimeoutException:
            logger.info("2FA step not required.")
            pass

        # ── Verify login ──────────────────────────────────────────────────────
        # Wait for account page, onboarding, or Google One to load
        WebDriverWait(driver, config.WEBDRIVER_TIMEOUT).until(
            lambda d: "myaccount.google.com" in d.current_url
            or "gds.google.com" in d.current_url
            or "one.google.com" in d.current_url
            or "signin" not in d.current_url
        )

        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        if (
            hostname in ["myaccount.google.com", "gds.google.com", "one.google.com"]
            or (hostname.endswith(".google.com") and "signin" not in path)
        ):
            logger.info("Login succeeded for %s (URL: %s)", email, current_url)
            return True

        # Check for error messages
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"], div[role="alert"]'
            )
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        logger.warning("Unexpected URL after login: %s", current_url)
        return False

    except TimeoutException as exc:
        logger.error("Timeout during login: %s", exc)
        return False
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(driver: uc.Chrome) -> Optional[str]:
    """
    Scan the current page for a Gemini Pro offer / activation link.

    Strategy:
    1. Look for anchor tags whose text or aria-label contains offer keywords.
    2. Fall back to scanning all links for 'gemini' or 'upgrade' patterns.
    3. Return the first matching href found.
    """
    keywords = config.GEMINI_OFFER_KEYWORDS

    # -- Strategy 1: anchor text / aria-label match ---------------------------
    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            text = (link.text + " " + link.get_attribute("aria-label")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text match: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 2: URL pattern scan -----------------------------------------
    url_patterns = re.compile(
        r"(gemini|upgrade|activate|offer|redeem|trial|checkout)",
        re.IGNORECASE,
    )
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL pattern: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 3: button / CTA elements ------------------------------------
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
    for btn in buttons:
        try:
            text = btn.text.lower()
            if any(kw in text for kw in keywords):
                # Try to find parent anchor
                try:
                    parent_link = btn.find_element(By.XPATH, "ancestor::a")
                    href = parent_link.get_attribute("href") or ""
                    if href:
                        logger.info("Found offer link via button parent: %s", href)
                        return href
                except NoSuchElementException:
                    pass
                # Return current URL as fallback (user will land on offer page)
                logger.info("Found offer CTA button on page: %s", driver.current_url)
                return driver.current_url
        except Exception:
            continue

    return None


def _navigate_google_one(driver: uc.Chrome) -> Optional[str]:
    """
    Navigate to Google One and attempt to find the Gemini Pro offer link.

    Returns the payment/activation URL or None if not found.
    """
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            # Dismiss cookie/consent banners if present
            for selector in (
                '[aria-label="Accept all"]',
                'button[jsname="higCR"]',
                '[data-action="accept"]',
            ): # Use a short timeout for optional elements
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click() # Click and continue without a fixed wait
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


async def check_gemini_offer(
    email: str,
    password: str,
    device: DeviceProfile,
    request_2fa_callback: Callable[[], Awaitable[str]],
) -> Optional[str]:
    """
    Main entry point.

    Logs into *email* / *password* using the supplied *device* profile,
    navigates to Google One, and returns the Gemini Pro offer link (or None).

    Raises :class:`GoogleAutomationError` if the driver cannot be started or the
    login step fails with an error.
    """
    driver: Optional[uc.Chrome] = None
    try:
        logger.info("Starting WebDriver for session %s", device.session_id)
        driver = _build_driver(device)

        logged_in = await _gmail_login(driver, email, password, request_2fa_callback)
        if not logged_in:
            raise GoogleAutomationError(
                "Login failed – please check your credentials."
            )

        offer_link = _navigate_google_one(driver)
        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
