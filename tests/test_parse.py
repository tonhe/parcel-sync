"""Parser tests modeled on real shipping-notice layouts.
All order and tracking numbers are made up. Run: python3 -m unittest -v
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from parcel_sync import extract_tracking, html_to_text  # noqa: E402

ABEBOOKS = """Better World Books: West has provided tracking information for AbeBooks Sales Order Number 100000001.

Shipping Company: USPS
Tracking Number: 9241990318400000123456

> Track Your Package
https://tools.usps.com/go/TrackConfirmAction_input?qtc_tLabels1=9241990318400000123456
http://www.abebooks.com/ask-bookseller-question/?refnum=100000002&reftype=po&cm_pla=order-tracking_CM
"""

APPLE = """Order Number W1000000001
Ordered on January 1, 2026
Your shipment is on its way.
Carrier Name
UPS
Tracking Number
1Z2E20720300000001
https://store.apple.com/vieworder/W1000000001/x/AOS
"""

TIKTOK = """| Part of your order is en route Some items from your TikTok Shop order shipped! |
| UPS Standard shipping |
| |
| 1Z84YF500300000002 |
| | Order number | 500000000000000001 | |
| logId: 20260101000000AAAAAAAAAAAAAAAAAAAA |
"""

SWEETWATER = """We're excited to tell you that your order L1000000001 shipped on 1/2/2026 via UPS Ground (Home)!
You can track your order here http://www.sweetwater.com/tracking/10000--20000000
Your order number is L1000000001
"""

FEDEX_HTML = """<p>Your package is on the way via FedEx.</p>
<p>Tracking number: <a href="https://www.fedex.com/fedextrack/?trknbr=771234567890">771234567890</a></p>
<p>Order #100234567</p>"""

FEDEX_LABELED_NO_LINK = "Shipped with FedEx Ground. Tracking #: 123456789012. Order 55512345678"

UNLABELED_DIGITS = "Your order 123456789012 has shipped. Questions? Call 8002224700."


NARVAR = """Your order is on its way! Track your package:
https://example.narvar.com/example/tracking/ups?tracking_numbers=1Z999AA10123456784&order_number=4455
"""

AFTERSHIP_FEDEX = """Shipped via FedEx. Click here to track your package
https://shop.aftership.com/track?tracking=771234560001&lang=en
"""


class ParseTests(unittest.TestCase):
    def test_narvar_param(self):
        self.assertEqual(extract_tracking(NARVAR), [("1Z999AA10123456784", "ups")])

    def test_aftership_param_uses_mail_wording_for_carrier(self):
        self.assertEqual(extract_tracking(AFTERSHIP_FEDEX), [("771234560001", "fedex")])

    def test_resolver_only_follows_allowlisted_hosts(self):
        import parcel_sync as ps
        seen = []
        orig = ps.resolve_link
        ps.resolve_link = lambda u: (seen.append(u), "https://www.ups.com/track?tracknum=1Z999AA10123456785")[1]
        try:
            text = ("Click here to track your package https://click.sendgrid.net/ls/abc123\n"
                    "Unsubscribe https://evil.example.com/track?x=1")
            self.assertEqual(ps.resolve_tracking_links(text), [("1Z999AA10123456785", "ups")])
            self.assertEqual(seen, ["https://click.sendgrid.net/ls/abc123"])
        finally:
            ps.resolve_link = orig

    def test_abebooks_usps(self):
        self.assertEqual(extract_tracking(ABEBOOKS), [("9241990318400000123456", "usps")])

    def test_apple_ups_next_line(self):
        self.assertEqual(extract_tracking(APPLE), [("1Z2E20720300000001", "ups")])

    def test_tiktok_ups_ignores_order_and_log_ids(self):
        self.assertEqual(extract_tracking(TIKTOK), [("1Z84YF500300000002", "ups")])

    def test_sweetwater_has_no_number(self):
        self.assertEqual(extract_tracking(SWEETWATER), [])

    def test_fedex_link_in_html(self):
        self.assertEqual(extract_tracking(html_to_text(FEDEX_HTML)), [("771234567890", "fedex")])

    def test_fedex_labeled(self):
        self.assertEqual(extract_tracking(FEDEX_LABELED_NO_LINK), [("123456789012", "fedex")])

    def test_unlabeled_digits_ignored(self):
        self.assertEqual(extract_tracking(UNLABELED_DIGITS), [])


if __name__ == "__main__":
    unittest.main()
