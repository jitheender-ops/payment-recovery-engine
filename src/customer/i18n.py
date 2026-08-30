"""
Strings for the customer recovery page, in the customer's language.

India's next few hundred million internet users are predominantly
non-English speakers, and a payment page they cannot fully read reads as
a scam: error messages in a second language produce panic and
abandonment, and the UPI app studies are unambiguous that interface
language drives security perception as strongly as the back end.

Structure: flat key → string catalogs, one per language. `pick()` resolves
the language from an explicit ?lang= override first (it must be possible
to force a language for support walks-throughs), then the Accept-Language
header, then English. English is the source of truth; every other
catalog must carry the same keys — a missing key renders the English string
rather than a blank, so a half-finished translation degrades to English
instead of to nothing.

Hindi strings here are a starting point written for clarity over
literalness; the research is explicit that machine-grade phrasing
undermines the trust this page exists to build, so they need a native
review before a real send. The catalog structure is the part that lasts —
adding Tamil, Telugu, Bengali or Marathi is one dict away.
"""

from __future__ import annotations

from typing import Any

SUPPORTED = ("en", "hi")

CATALOGS: dict[str, dict[str, str]] = {
    "en": {
        # masthead / hero
        "masthead": "Payment recovery",
        "hero_about": "About your payment to",
        # Per-risk-type hero labels — three of these never attempted a
        # payment, so calling it one would be a lie on the first line.
        "hero_about_order": "About your order from",
        "hero_about_subscription": "About your subscription with",
        "hero_about_invoice": "About your invoice from",
        "hero_about_mandate": "About your autopay to",
        # rail legend (the custody rail's three stops)
        "rail_you": "Your account",
        "rail_transit": "In transit",
        "rail_merchant": "Merchant",
        # sections
        "sec_what_happened": "What happened",
        "sec_what_to_do": "What to do",
        "sec_this_payment": "This payment",
        "sec_help": "Need a hand?",
        "sec_receipt": "Receipt",
        # receipt (recovered state)
        "receipt_paid": "Amount paid",
        "receipt_reference": "Payment reference",
        "receipt_received": "Received on",
        # FAQ
        "faq_charged_q": "I was charged, but this page says failed",
        "faq_safe_q": "Is it safe to pay here?",
        "faq_safe_a": (
            "Payment is handled by Razorpay. We never see or store your card "
            "details. This page shows only your own payment and the link "
            "expires on its own."
        ),
        "faq_charged_a": (
            "Rarely, a slow bank confirms a payment after it reported failure. "
            "If money left your account, it is either a temporary hold "
            "(released in 3 to 5 working days) or an already-successful "
            "payment updating out of order. Either way you are never charged "
            "twice for this reference."
        ),
        # timeline rows (payable state)
        "timeline_attempted": "Payment attempted",
        "timeline_attempted_at": "on {date}",
        "timeline_result": "The bank declined it",
        "timeline_result_reason": "The bank declined it — {reason}",
        "timeline_safe": "Retrying is safe. No money has left your account.",
        # timeline rows for the chaser-driven risk types — three of these
        # never attempted a payment, so the row says what actually happened.
        "timeline_risk_order": "Your order was left incomplete",
        "timeline_risk_subscription": "The renewal charge didn't go through",
        "timeline_risk_invoice": "The due date passed without payment",
        "timeline_risk_mandate": "The autopay debit didn't go through",
        # cart line (payable state, carts only): what the order contains.
        # Rendered only when the merchant's event actually named the items.
        "cart_items": "In your order: {items}",
        # register labels
        "register_attempted": "Attempted",
        "register_opened": "Opened",
        # actions
        "pay_securely": "Pay {amount} securely",
        "pay_upi": "Pay {amount} by UPI",
        "pay_opening": "Opening Razorpay…",
        "pay_note": "Opens Razorpay. You'll come back here once it's done.",
        "pay_other_methods": "You can choose a different payment method on the next screen.",
        # trust strip (sits at the CTA, not the footer)
        "trust_secured": "Payments secured by Razorpay",
        "trust_never_stored": "Your card details are never seen or stored by us",
        # expiry (true deadline: the token dies with the consent window)
        "expires_line": "This link works until {when}.",
        # confirming / unknown
        "check_again": "Check again",
        "confirming_auto": "This page will check again by itself in a few seconds.",
        "unknown_sms": (
            "We'll message you the moment it's confirmed. "
            "You don't need to do anything."
        ),
        # help & control
        "help_whatsapp": "Talk to us on WhatsApp",
        "help_footer": "Trouble with this payment? Reply to the message we sent you and quote",
        "opt_out": "Don't contact me about this payment",
        "opt_out_done": "We've stopped contacting you",
        # language names, shown in the toggle
        "lang_name": "English",
    },
    "hi": {
        "masthead": "भुगतान वसूली",
        "hero_about": "आपका भुगतान",
        "hero_about_order": "आपका ऑर्डर",
        "hero_about_subscription": "आपकी सब्सक्रिप्शन",
        "hero_about_invoice": "आपका इनवॉइस",
        "hero_about_mandate": "आपका ऑटोपे",
        "faq_charged_q": "मेरे खाते से पैसे कटे, लेकिन यह पेज 'विफल' दिखा रहा है",
        "faq_charged_a": (
            "कभी-कभी धीमा बैंक विफलता दिखाने के बाद भुगतान की पुष्टि कर देता है। "
            "यदि पैसे कटे हैं, तो वे या तो अस्थायी होल्ड हैं (3 से 5 कार्यदिवसों में "
            "वापस) या एक सफल भुगतान जो देर से अपडेट हुआ। किसी भी स्थिति में इसी "
            "संदर्भ के लिए आपसे दोबारा चार्ज नहीं किया जाएगा।"
        ),
        "faq_safe_q": "क्यो यहॉ भुगतान करने क्या सुरक्षित है?",
        "faq_safe_a": (
            "भुगतान Razorpay द्वारा संभाला जाता है। हम आपके कार्ड विवरण कभी नहीं देखते या संग्रहीत करते हैं। "
            "यह पेज केवल आपका अपना भुगतान दिखाता है और लिंक अपने आप समाप्त हो जाता है।"
        ),
        "rail_you": "आपका खाता",
        "rail_transit": "रास्ते में",
        "rail_merchant": "व्यापारी",
        "sec_what_happened": "क्या हुआ",
        "sec_what_to_do": "क्या करें",
        "sec_this_payment": "यह भुगतान",
        "sec_help": "मदद चाहिए?",
        "sec_receipt": "रसीद",
        "receipt_paid": "भुगतान राशि",
        "receipt_reference": "भुगतान संदर्भ",
        "receipt_received": "प्राप्त हुआ",
        "timeline_attempted": "भुगतान की कोशिश हुई",
        "timeline_attempted_at": "{date} को",
        "timeline_result": "बैंक ने भुगतान रोक दिया",
        "timeline_result_reason": "बैंक ने भुगतान रोक दिया — {reason}",
        "timeline_safe": "दोबारा कोशिश करना सुरक्षित है। आपके खाते से कोई पैसा नहीं गया है।",
        "timeline_risk_order": "आपका ऑर्डर अधूरा रह गया",
        "timeline_risk_subscription": "रिन्युअल चार्ज नहीं हो पाया",
        "timeline_risk_invoice": "बिना भुगतान के डेट निकल गई",
        "timeline_risk_mandate": "ऑटोपे डेबिट नहीं हो पाया",
        "cart_items": "आपके ऑर्डर में: {items}",
        "register_attempted": "कोशिश की गई",
        "register_opened": "खोला गया",
        "pay_securely": "{amount} सुरक्षित रूप से भुगतान करें",
        "pay_upi": "{amount} UPI से भुगतान करें",
        "pay_opening": "Razorpay खुल रहा है…",
        "pay_note": "Razorpay खुलेगा। भुगतान होते ही आप यहीं वापस आ जाएंगे।",
        "pay_other_methods": "अगली स्क्रीन पर आप कोई और भुगतान तरीका चुन सकते हैं।",
        "trust_secured": "भुगतान Razorpay के ज़रिए सुरक्षित है",
        "trust_never_stored": "आपके कार्ड की जानकारी हम कभी देखते या सेव नहीं करते",
        "expires_line": "यह लिंक {when} तक चलेगा।",
        "check_again": "फिर जाँचें",
        "confirming_auto": "यह पेज कुछ सेकंड में खुद दोबारा जाँच लेगा।",
        "unknown_sms": "पक्का होते ही हम आपको संदेश भेज देंगे। आपको कुछ करने की ज़रूरत नहीं है।",
        "help_whatsapp": "WhatsApp पर हमसे बात करें",
        "help_footer": "भुगतान में दिक्कत है? हमारे भेजे संदेश का जवाब दें और यह हवाला दें",
        "opt_out": "इस भुगतान के बारे में मुझे संपर्क न करें",
        "opt_out_done": "हमने आपसे संपर्क करना बंद कर दिया है",
        "lang_name": "हिंदी",
    },
}


def pick(request_lang_param: str | None, accept_language: str | None) -> str:
    """
    Resolve the page language. Explicit ?lang= wins (support needs to walk
    someone through a page in a fixed language), then the device's
    Accept-Language, then English.
    """
    if request_lang_param in SUPPORTED:
        return request_lang_param
    if accept_language:
        for part in accept_language.lower().split(","):
            code = part.split(";")[0].strip()[:2]
            if code in SUPPORTED:
                return code
    return "en"


class Translator:
    """t("key", **kw) → the string in the resolved language, English-formatted."""

    def __init__(self, lang: str) -> None:
        self.lang = lang if lang in SUPPORTED else "en"
        self._catalog = CATALOGS[self.lang]
        self._english = CATALOGS["en"]

    def __call__(self, key: str, **kw: Any) -> str:
        text = self._catalog.get(key) or self._english.get(key)
        if text is None:
            return key
        if kw:
            # No brace-escaping of the VALUES. str.format never re-processes
            # what it substitutes, so escaping there could not prevent an error
            # — it only rendered a merchant name containing "{" as literal
            # "{{" on the customer's page. The templates are our own catalog
            # strings, which is where a stray brace would actually matter.
            text = text.format(**{k: str(v) for k, v in kw.items()})
        return text

    @property
    def other(self) -> tuple[str, str]:
        """(code, native name) of the other supported language, for the toggle."""
        other = "hi" if self.lang == "en" else "en"
        return other, CATALOGS[other].get("lang_name", other)
