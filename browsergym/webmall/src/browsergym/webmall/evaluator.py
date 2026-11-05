from playwright.sync_api import Page, Error as PlaywrightError
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import requests
from .checkpoints import Checklist, Checkpoint
import os
import logging
import re
import time

logger = logging.getLogger(__name__)
PATH_TO_DOT_ENV_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "..", ".env")
)


class Evaluator():
    def __init__(self):
        self.done = False
        pass

    def _normalize_url(self, url):
        return url.rstrip("/").lstrip("http://").lstrip("https://")
    
    def _get_domain(self, url: str) -> str:
        """Extract domain from URL"""
        parsed = urlparse(url)
        return parsed.netloc

    def _page_content(self, page: Page, attempts: int = 3) -> str:
        """page.content() that waits out a navigation still in progress instead of raising."""
        for attempt in range(attempts):
            try:
                return page.content()
            except PlaywrightError as e:
                if "navigating" not in str(e) or attempt == attempts - 1:
                    raise
                page.wait_for_timeout(500)
                _wait_for_load(page)


def _wait_for_load(page: Page, timeout: int = 10_000) -> None:
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except PlaywrightError:
        pass  # a page that never finishes loading is evaluated as it is


class RequestTracker():
    """Tracks the browser context's in-flight document/XHR/fetch requests.

    An action can start a navigation that lands only after BrowserGym considers the action done,
    e.g. WooCommerce's AJAX checkout redirecting to order-received; evaluating before that misses
    the final page or fails with "page is navigating". Playwright's "networkidle" does not cover
    this: it only fires once per navigation, not for requests made later.
    """
    TRACKED_TYPES = {"document", "xhr", "fetch"}

    def __init__(self, context):
        self.pending = set()
        context.on("request", self._on_start)
        context.on("requestfinished", self._on_end)
        context.on("requestfailed", self._on_end)

    def _on_start(self, request):
        if request.resource_type in self.TRACKED_TYPES:
            self.pending.add(request)

    def _on_end(self, request):
        self.pending.discard(request)

    def wait_until_settled(self, page: Page, quiet_ms: int = 500, timeout_ms: int = 10_000) -> None:
        """Wait until no tracked request has been pending for `quiet_ms`, then for the page load.

        The quiet window covers the gap between an AJAX response and the redirect it triggers.
        """
        start = time.monotonic()
        quiet_since = None
        while (time.monotonic() - start) * 1000 < timeout_ms:
            if self.pending:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
                break
            page.wait_for_timeout(50)  # also lets Playwright dispatch the request events
        else:
            logger.warning(f"Requests still pending after {timeout_ms} ms: {[r.url for r in self.pending]}")
        _wait_for_load(page)

URL_RE = re.compile(
    r"""\b
        (?:https?://)?                       # optional http:// or https://
        (?:                                  # ── host ─────────────────────
              localhost                      #   localhost
            | (?:\d{1,3}(?:\.\d{1,3}){3})    #   IPv4 like 127.0.0.1
            | (?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,} # domain.tld (with sub-domains)
        )
        (?::\d{2,5})?                        # optional :port
        (?:/[^\s]*)?                         # optional /path?query#fragment
        \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

class StringEvaluator(Evaluator):
    def eval(self, last_message, page: Page, checkpoints: list[Checkpoint]) -> float:
        score = 0
        final_text = self._fetch_final_text(page)
        if not final_text:
            return score, []
        
        self.done = True
        # replace ### with newlines to ensure regex works
        final_text = final_text.replace("###", "\n")
        # Extract all URLs from the submission
        submitted_urls = [m.group(0) for m in URL_RE.finditer(final_text)]
        print("FOUND SUBMITTED URLS:", submitted_urls)

        # Normalize checkpoints
        checkpoint_map = {self._normalize_url(cp.value): cp for cp in checkpoints}

        # Track matched checkpoints
        matched_checkpoints = set()

        wrong_solutions = []

        normalized_to_original = {
            self._normalize_url(submitted_url): submitted_url
            for submitted_url in submitted_urls
        }
        unique_normalized_urls = set(normalized_to_original.keys())

        for norm_submitted_url in unique_normalized_urls:
            for norm_cp, cp in checkpoint_map.items():
                if norm_cp == norm_submitted_url:
                    if not cp.flag:
                        cp.flag = True
                        score += cp.weight
                        logger.info(f"[StringEvaluator] Matched checkpoint: {cp}")
                        matched_checkpoints.add(cp.id)
                        break
            else:
                wrong_solutions.append(normalized_to_original[norm_submitted_url])
                logger.info(f"[StringEvaluator] Wrong submission: {normalized_to_original[norm_submitted_url]}")
        return score, wrong_solutions

    def _fetch_final_text(self, page: Page) -> str:
            """Return the text that was written to #submittedResult on the WebMall page."""
            if page.url.rstrip("/") == read_frontend_url(PATH_TO_DOT_ENV_FILE).rstrip("/"):
                try:
                    element = page.wait_for_selector("#submittedResult", state="attached")
                    text = element.text_content() or ""
                    return text.strip()
                except Exception as exc:
                    logger.error("[StringEvaluator] Could not fetch final result: %s", exc)
                    return ""
            return ""
            
def read_frontend_url(file_path: str = ".env") -> str:
    def clean(val: str) -> str:
        return val.strip().strip('"').strip("'")

    # Try from environment
    val = os.getenv("FRONTEND_URL")
    if val:
        return clean(val)

    # Try from .env file
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if key.strip() == "FRONTEND_URL" and sep:
                    return clean(value)

    raise ValueError(f"FRONTEND_URL not found in environment or {file_path}")

class CheckoutEvaluator(Evaluator):
    def eval(self, last_message, page: Page, checkpoints: list[Checkpoint]):
        if "/checkout/order-received" not in page.url:
            return 0.0, []

        soup = BeautifulSoup(self._page_content(page), "html.parser")
        score = 0.0
        wrong_products = []

        # Create slug-to-checkpoint mapping
        expected_slugs = {self._slug(cp.value): cp for cp in checkpoints if cp.type == "checkout"}

        # Extract all slugs from the order confirmation page
        found_slugs = self._extract_product_slugs_from_order(soup)

        # Match slugs against checkpoints
        for slug, cp in expected_slugs.items():
            if slug in found_slugs:
                # Confirm full match: product + billing + email
                if self._billing_ok(soup, getattr(cp, "user_details", None)):
                        if self._email_ok(soup, getattr(cp, "user_details", None)["email"]):
                            if not cp.flag:
                                cp.flag = True
                                score += cp.weight
                                logger.info(f"[CheckoutEvaluator] Triggered checkpoint: {cp}")
                found_slugs.discard(slug)  # remove matched slug

        # Anything left in found_slugs was not expected → wrong product
        wrong_products = list(found_slugs)

        return score, wrong_products

    def _slug(self, url: str) -> str:
        # Extract the slug from a product URL, e.g. /product/my-product -> my-product
        return urlparse(url).path.rstrip("/").split("/")[-1]

    def _extract_product_slugs_from_order(self, soup: BeautifulSoup) -> set[str]:
        slugs = set()

        selectors = [
            'td.woocommerce-table__product-name a[href]',  # Classic theme
            'td.wc-block-order-confirmation-totals__product a[href]',  # Block theme
        ]

        for selector in selectors:
            for a in soup.select(selector):
                href = a.get("href")
                if href:
                    slugs.add(self._slug(href))

        return slugs

    def _billing_ok(self, soup: BeautifulSoup, details: dict | None) -> bool:
        block = (
            soup.select_one(".woocommerce-customer-details address")
            or soup.select_one('[data-block-name="woocommerce/order-confirmation-billing-address"] address')
            or soup.find("address")
        )

        text = block.get_text(" ", strip=True).lower()

        def in_text(value: str) -> bool:
            return str(value).strip().lower() in text

        name_ok = in_text(details.get("name", ""))
        street_ok = in_text(details.get("street", ""))
        house_number_ok = in_text(details.get("house_number", ""))
        zip_ok = in_text(details.get("zip", ""))
        state_ok = in_text(details.get("state", ""))

        country = details.get("country", "").lower()
        country_options = [country, "us", "usa", "united states"]
        country_ok = any(c in text for c in country_options)

        all_ok = all([name_ok, street_ok, house_number_ok, zip_ok, state_ok, country_ok])

        return all_ok

    def _email_ok(self, soup: BeautifulSoup, expected_email: str) -> bool:
        return expected_email.lower() in soup.get_text().lower()


class CartEvaluator(Evaluator):
    """Detects whether the correct product has been added to the cart."""

    def eval(self, last_message, page: Page, checkpoints: list[Checkpoint]):
        soup = BeautifulSoup(self._page_content(page), "html.parser")
        score = 0
        wrong_products = []

        # Map slugs to list of checkpoints (handle duplicate slugs)
        slug_to_cps = {}
        for cp in checkpoints:
            slug = self._slug(cp.value)
            if slug:
                if slug not in slug_to_cps:
                    slug_to_cps[slug] = []
                slug_to_cps[slug].append(cp)

        # Get current page domain
        current_domain = self._get_domain(page.url)

        # Get all slugs detected via cart page, product banner, or overview
        detected_slugs = set()

        for slug in slug_to_cps.keys():
            if (
                self._on_product_page_with_banner(page.url, soup, slug)
                or self._product_in_cart_page(page.url, soup, slug)
                or self._product_added_in_overview(soup, slug)
            ):
                detected_slugs.add(slug)

        # Evaluate each detected slug
        for slug in detected_slugs:
            cps = slug_to_cps.get(slug, [])
            matched_any = False
            for cp in cps:
                # Check if both slug and domain match
                cp_domain = self._get_domain(cp.value)
                if cp_domain == current_domain and not cp.flag:
                    cp.flag = True
                    score += cp.weight
                    logger.info(f"[CartEvaluator] Triggered checkpoint: {cp}")
                    matched_any = True
            
            if not matched_any:
                wrong_products.append(slug)

        return score, wrong_products

    # ───────────────── helpers ─────────────────
    def _slug(self, url: str) -> str:
        return urlparse(url).path.rstrip("/").split("/")[-1]

    def _on_product_page_with_banner(self, url: str, soup, slug: str) -> bool:
        if not (soup.select_one(".woocommerce-message") or soup.select_one(".wc-block-components-notice-banner__content")):
            return False
        return slug in url

    def _product_in_cart_page(self, url: str, soup, slug: str) -> bool:
        if "/cart" not in url:
            return False
        
        for row in soup.select("tr.wc-block-cart-items__row, tr.cart_item, td.product-name"):
            a = row.find("a", href=True)
            if a and slug in a["href"]:
                return True
        return False

    def _product_added_in_overview(self, soup, slug: str) -> bool:
        for li in soup.select("li.product"):
            link = li.find("a", href=True)
            if link and slug in link["href"]:
                if li.find("a", class_="added_to_cart"):
                    return True
        return False
    

class URLEvaluator(Evaluator):
     def eval(self, last_message, page: Page, checkpoints: list[Checkpoint]):
         score = 0
         for cp in checkpoints:
             expected = cp.value
             if self._normalize_url(expected) in self._normalize_url(page.url) and not cp.flag:
                 cp.flag = True
                 score += cp.weight
                 logger.info(f"[URLEvaluator] Triggered checkpoint: {cp}")
         return score, []
        

class EvaluatorComb():
    def __init__(self, evaluators: list[Evaluator]):
        self.evaluators = evaluators
        self.done = False
    def score(self, last_message, page: Page, checklist: Checklist):
        total_score = 0
        by_type = checklist.group_by_type()
        wrong_solutions = []
        for evaluator in self.evaluators:
            eval_type = evaluator.__class__.__name__.replace("Evaluator", "").lower()
            eval_type = "string" if eval_type == "" else eval_type  # fallback

            cps = [cp for cp in by_type.get(eval_type, [])] # filter for checkpoints with specific eval type
            score, wrong_solution = evaluator.eval(last_message, page, cps)
            total_score += score
            wrong_solutions = wrong_solutions + wrong_solution
            self.done = evaluator.done or self.done

        return total_score, checklist, wrong_solutions






