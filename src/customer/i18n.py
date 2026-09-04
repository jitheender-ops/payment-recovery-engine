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
        # retry-sequence rows (subscription/mandate only) — one label per
        # attempt's own action_type, honest about what happened, never what
        # will happen next (the upcoming row deliberately says WHEN only).
        "sequence_action_retry": "We tried to collect payment.",
        "sequence_action_switch_rail": "We suggested a different way to pay.",
        "sequence_action_nudge": "We sent you a reminder.",
        "sequence_upcoming": "We'll be in touch again around {when}.",
        # honest voice-call trace — any risk type, whenever a call actually
        # completed (VoiceCallQueue.state == "done")
        "sequence_call_made": "We called you on {when}.",
        # register labels
        "register_attempted": "Attempted",
        "register_opened": "Opened",
        # Invoice aging — computed from the case's own due_at, the same
        # figure the ladder escalates on. Never rendered before the date
        # passes: there is no honest "0 days overdue".
        "register_due": "Due",
        "days_overdue": "{days} days overdue",
        # actions
        "pay_securely": "Pay {amount} securely",
        "pay_upi": "Pay {amount} by UPI",
        "pay_opening": "Opening Razorpay…",
        "pay_note": "Opens Razorpay. You'll come back here once it's done.",
        "pay_other_methods": "You can choose a different payment method on the next screen.",
        # Rail recommendation — the engine's own switch_rail decision, said
        # out loud instead of only showing up as a changed button verb.
        # Rendered only when the recommended rail differs from the one that
        # failed; computed from recommended_rail, never invented.
        "rail_recommend_upi": "We recommend UPI for this payment.",
        "rail_recommend_upi_why": "It skips the card step that failed.",
        # trust strip (sits at the CTA, not the footer)
        "trust_secured": "Payments secured by Razorpay",
        "trust_never_stored": "Your card details are never seen or stored by us",
        # expiry (true deadline: the token dies with the consent window)
        "expires_line": "This link works until {when}.",
        # promise persistence (server-computed, never fabricated)
        "promise_pending": "You said you'd pay by {when}.",
        "promise_days_left": "{days} days left.",
        "promise_due_today": "Due today.",
        "promise_broken": "Your last promised date passed without payment.",
        # autopay on a promise. Exact about what is authorised — one amount,
        # one date, one debit — because this is the screen where a customer
        # hands over a standing instruction, and vagueness here is the thing
        # a chargeback is about.
        "autopay_offer": "Pay it automatically on that date",
        "autopay_detail": (
            "Approve once in your UPI app. We'll collect ₹{amount} on that "
            "date. Nothing before it, nothing more, nothing after."
        ),
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
        # The statement page stops contact about the whole account — the
        # underlying opt-out always did (record_opt_out closes every open
        # case for the customer), so the per-payment wording understated it
        # wherever the reader is looking at more than one invoice.
        "opt_out_account": "Don't contact me about this account",
        "opt_out_done": "We've stopped contacting you",
        # ── The customer home: everything one person has open with us ──────
        "mine_title": "Everything you owe",
        "mine_lede": (
            "Every payment of yours we are still waiting on, in one place. "
            "Open any one to pay it, promise a date, ask to split it, or tell "
            "us something is wrong."
        ),
        "mine_total": "Still owed in total",
        "mine_across": "across {n} payments",
        "mine_across_one": "on one payment",
        "mine_open": "Open",
        "mine_settled": "Already settled",
        "mine_settled_lede": (
            "Nothing to do here — kept for your records."
        ),
        "mine_nothing": "You are all paid up.",
        "mine_nothing_lede": (
            "There is nothing outstanding with us right now. If you were "
            "expecting to see something, reply to the message we sent you."
        ),
        "mine_promised": "You promised {date}",
        "mine_plan": "Paying in {n} parts",
        "mine_disputed": "Under review — we've paused reminders",
        "mine_part_paid": "{amount} already paid",
        "mine_pay": "Open",
        "mine_expires": "This page works until {when}.",
        "mine_one_link": (
            "This link is yours. Anyone you forward it to can see this page, "
            "so send it on only if you mean to."
        ),
        "mine_each_opens": (
            "Each one opens its own page, where you can pay it, promise a "
            "date, ask to pay in parts, or tell us something is wrong. "
            "Nothing is charged from this page."
        ),
        "mine_footer": "Questions? Reply to the message we sent you.",
        "mine_see_all": "See everything you owe us",
        "mine_see_all_n": "See all {n} payments you have with us",
        "opt_out_all": "Don't contact me about any of these",
        # language names, shown in the toggle
        "lang_name": "English",
    },
    "hi": {
        "masthead": "भुगतान वसूली",
        "mine_title": "आपके सभी बकाया",
        "mine_lede": (
            "आपके वे सभी भुगतान जिनका हमें इंतज़ार है, एक ही जगह। किसी को भी "
            "खोलकर भुगतान करें, तारीख़ बताएँ, किस्तों में बाँटने को कहें, या "
            "कोई दिक़्क़त बताएँ।"
        ),
        "mine_total": "कुल बकाया",
        "mine_across": "{n} भुगतानों पर",
        "mine_across_one": "एक भुगतान पर",
        "mine_open": "बकाया",
        "mine_settled": "निपट चुके",
        "mine_settled_lede": "यहाँ कुछ करना नहीं है — सिर्फ़ आपके रिकॉर्ड के लिए।",
        "mine_nothing": "आपका कोई बकाया नहीं है।",
        "mine_nothing_lede": (
            "अभी हमारे पास आपका कुछ भी बकाया नहीं है। अगर आपको कुछ दिखना "
            "चाहिए था, तो हमारे भेजे संदेश का जवाब दें।"
        ),
        "mine_promised": "आपने {date} का वादा किया",
        "mine_plan": "{n} किस्तों में",
        "mine_disputed": "समीक्षा में — हमने याद दिलाना रोक दिया है",
        "mine_part_paid": "{amount} पहले ही चुकाया",
        "mine_pay": "खोलें",
        "mine_expires": "यह पेज {when} तक चलेगा।",
        "mine_one_link": (
            "यह लिंक आपका है। जिसे भी आप इसे भेजेंगे वह यह पेज देख सकेगा, "
            "इसलिए सोच-समझकर ही आगे भेजें।"
        ),
        "mine_each_opens": (
            "हर एक अपना अलग पेज खोलता है, जहाँ आप भुगतान कर सकते हैं, तारीख़ "
            "बता सकते हैं, किस्तों में बाँटने को कह सकते हैं, या कोई दिक़्क़त "
            "बता सकते हैं। इस पेज से कुछ भी नहीं काटा जाता।"
        ),
        "mine_footer": "कोई सवाल? हमारे भेजे संदेश का जवाब दें।",
        "mine_see_all": "अपने सभी बकाया देखें",
        "mine_see_all_n": "हमारे साथ अपने सभी {n} भुगतान देखें",
        "opt_out_all": "इनमें से किसी के बारे में मुझसे संपर्क न करें",
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
        "sequence_action_retry": "हमने भुगतान लेने की कोशिश की।",
        "sequence_action_switch_rail": "हमने भुगतान का एक अलग तरीका सुझाया।",
        "sequence_action_nudge": "हमने आपको याद दिलाया।",
        "sequence_upcoming": "हम {when} के आसपास फिर संपर्क करेंगे।",
        "sequence_call_made": "हमने आपको {when} को कॉल किया था।",
        "register_attempted": "कोशिश की गई",
        "register_opened": "खोला गया",
        "register_due": "देय तिथि",
        "days_overdue": "{days} दिन बीत चुके हैं",
        "pay_securely": "{amount} सुरक्षित रूप से भुगतान करें",
        "pay_upi": "{amount} UPI से भुगतान करें",
        "pay_opening": "Razorpay खुल रहा है…",
        "pay_note": "Razorpay खुलेगा। भुगतान होते ही आप यहीं वापस आ जाएंगे।",
        "pay_other_methods": "अगली स्क्रीन पर आप कोई और भुगतान तरीका चुन सकते हैं।",
        "rail_recommend_upi": "इस भुगतान के लिए हम UPI का सुझाव देते हैं।",
        "rail_recommend_upi_why": "इससे वह कार्ड वाला चरण छूट जाता है जो विफल हुआ था।",
        "trust_secured": "भुगतान Razorpay के ज़रिए सुरक्षित है",
        "trust_never_stored": "आपके कार्ड की जानकारी हम कभी देखते या सेव नहीं करते",
        "expires_line": "यह लिंक {when} तक चलेगा।",
        "promise_pending": "आपने {when} तक भुगतान करने का वादा किया था।",
        "promise_days_left": "{days} दिन बाकी हैं।",
        "promise_due_today": "आज आखिरी दिन है।",
        "promise_broken": "आपकी पिछली वादा तारीख बिना भुगतान के निकल गई।",
        "autopay_offer": "उसी तारीख को अपने आप भुगतान हो जाए",
        "autopay_detail": (
            "अपने UPI ऐप में एक बार मंज़ूरी दें। हम उसी तारीख को ₹{amount} "
            "लेंगे। न उससे पहले, न ज़्यादा, न दोबारा।"
        ),
        "check_again": "फिर जाँचें",
        "confirming_auto": "यह पेज कुछ सेकंड में खुद दोबारा जाँच लेगा।",
        "unknown_sms": "पक्का होते ही हम आपको संदेश भेज देंगे। आपको कुछ करने की ज़रूरत नहीं है।",
        "help_whatsapp": "WhatsApp पर हमसे बात करें",
        "help_footer": "भुगतान में दिक्कत है? हमारे भेजे संदेश का जवाब दें और यह हवाला दें",
        "opt_out": "इस भुगतान के बारे में मुझे संपर्क न करें",
        "opt_out_account": "इस खाते के बारे में मुझे संपर्क न करें",
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
