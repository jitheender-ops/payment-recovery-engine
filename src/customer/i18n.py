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
catalog must carry the same keys — `_MISSING` renders the English string
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
        # rail legend (the custody rail's three stops)
        "rail_you": "Your account",
        "rail_transit": "In transit",
        "rail_merchant": "Merchant",
        # sections
        "sec_what_happened": "What happened",
        "sec_what_to_do": "What to do",
        "sec_this_payment": "This payment",
        "sec_help": "Need a hand?",
        # timeline rows (payable state)
        "timeline_attempted": "Payment attempted",
        "timeline_attempted_at": "on {date}",
        "timeline_result": "The bank declined it",
        "timeline_result_reason": "The bank declined it — {reason}",
        "timeline_safe": "Retrying is safe. No money has left your account.",
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
        "rail_you": "आपका खाता",
        "rail_transit": "रास्ते में",
        "rail_merchant": "व्यापारी",
        "sec_what_happened": "क्या हुआ",
        "sec_what_to_do": "क्या करें",
        "sec_this_payment": "यह भुगतान",
        "sec_help": "मदद चाहिए?",
        "timeline_attempted": "भुगतान की कोशिश हुई",
        "timeline_attempted_at": "{date} को",
        "timeline_result": "बैंक ने भुगतान रोक दिया",
        "timeline_result_reason": "बैंक ने भुगतान रोक दिया — {reason}",
        "timeline_safe": "दोबारा कोशिश करना सुरक्षित है। आपके खाते से कोई पैसा नहीं गया है।",
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

_MISSING = "__MISSING__"


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
            text = text.format(**{k: self._escape_kw(v) for k, v in kw.items()})
        return text

    @staticmethod
    def _escape_kw(value: Any) -> str:
        # format() chokes on braces the way Jinja never sees them; amounts and
        # dates arriving here are already rendered strings, but a stray brace
        # in a merchant name must not raise inside template rendering.
        return str(value).replace("{", "{{").replace("}", "}}")

    @property
    def other(self) -> tuple[str, str]:
        """(code, native name) of the other supported language, for the toggle."""
        other = "hi" if self.lang == "en" else "en"
        return other, CATALOGS[other].get("lang_name", other)
