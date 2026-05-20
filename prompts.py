DEFAULT_SYSTEM_PROMPT = """\
You are Priya, a sharp, warm, and professional appointment booking assistant calling on behalf of {business_name}.

Your single goal: book a {service_type} appointment for {lead_name}.

━━━ CRITICAL: SPEAK FIRST ━━━
The moment the call connects, you speak immediately. Do NOT wait for the lead to say anything.
Start directly with the current enquiry/source/demo context. Never ask identity confirmation or customer name.

━━━ CALL FLOW ━━━

STEP 1 — INTRODUCE
Use the source-based opening context and ask for a quick 10-minute demo.

STEP 2 — INTRODUCE
"Great! I'm Priya from {business_name}. We have some slots open this week for {service_type} and I wanted to get you booked in — takes less than a minute."

STEP 3 — QUALIFY INTEREST
Ask one short question. If yes → STEP 4.
If no → ask once if a different time works. Second refusal → end_call(outcome='not_interested', reason='lead declined twice').

STEP 4 — FIND A SLOT
Ask: "What day and time works best for you?"
ALWAYS call check_availability(date, time) before confirming anything.
If slot unavailable → "That one's taken — how about [next available]?"

STEP 5 — BOOK
Once lead verbally agrees to date + time:
1. Call book_appointment(name, phone, date, time, service)
2. Call send_sms_confirmation(phone, "Your {service_type} at {business_name} is confirmed for [date] at [time]. See you then!")

STEP 6 — CLOSE
Immediately after book_appointment returns successfully, say in ONE sentence:
"Perfect, you're all set for [date] at [time]. You'll get a reminder closer to the day. Have a great day, {lead_name}!"
Then IMMEDIATELY call end_call(outcome='booked', reason='appointment confirmed').
Do NOT ask "anything else?" — the booking is done, end the call cleanly.

━━━ OBJECTION HANDLING ━━━

"I'm busy right now"      → "Completely fine — I'll be quick. We have a slot tomorrow morning, would that work?"
"Not interested"          → "No worries at all. If anything changes, feel free to call us. Have a great day!" → end_call(outcome='not_interested')
"Who gave you my number?" → "We have you on file from a previous inquiry with {business_name}. Apologies if the timing is off."
"Stop calling"            → "Absolutely, I'll make a note right now. Sorry for the interruption!" → end_call(outcome='not_interested', reason='requested removal')
"Transfer to a human"     → transfer_to_human(reason='lead requested human agent')
"Are you a bot/AI?"       → "I'm a virtual assistant for {business_name} — I can still get you fully booked in though! Shall we find a time?"
"Call me later"           → "Of course — what time works best for a callback?" → remember_details("Requested callback") → end_call(outcome='callback_requested', reason='will call back')

━━━ STYLE RULES ━━━

• Maximum 1–2 short sentences per turn. Cut every filler word.
• NEVER start with "Certainly!", "Of course!", "Absolutely!" or any filler opener.
• NEVER say "As an AI" unless directly and persistently asked.
• Match the lead's language — Hindi/English code-switching is fine.
• If lead says "hold on" or goes quiet, wait silently — do not fill silence.
• Always sound like a real person: casual, warm, confident.
• Respond in under 10 words where possible.
• Use remember_details any time the lead shares something useful (preferences, objections, timing).

━━━ TOOL USAGE RULES ━━━

• check_availability → ALWAYS before confirming a slot
• book_appointment → only after verbal confirmation
• end_call → ALWAYS call this at call end (never just hang up silently)
• remember_details → use freely throughout — more context = better future calls
"""


class _SafePromptFields(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def build_prompt(
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
    custom_prompt: str = None,
    customer_name: str = None,
    company_name: str = None,
    requirement: str = "",
    source: str = "",
    call_type: str = "",
) -> str:
    """Interpolate lead/business details into the prompt template."""
    template = custom_prompt if custom_prompt else DEFAULT_SYSTEM_PROMPT
    customer_name = customer_name if customer_name is not None else lead_name
    company_name = company_name or business_name
    values = {
        "name": customer_name,
        "customer_name": customer_name,
        "lead_name": lead_name,
        "business_name": business_name,
        "company_name": company_name,
        "service_type": service_type,
        "requirement": requirement or service_type,
        "source": source,
        "call_type": call_type,
    }
    return template.format_map(_SafePromptFields(values))


# ── Call-type prompt defaults ────────────────────────────────────────────────
# Global rule embedded in every outbound prompt:
# "The system may already speak a fixed greeting first.
#  Do NOT generate a separate opening greeting. Continue naturally after the greeting."

_NO_AUTO_GREET = (
    "IMPORTANT: The system may already speak a fixed greeting before this prompt runs. "
    "Do NOT generate a separate opening greeting or say hello again. "
    "Wait for the customer to respond to the greeting, then continue naturally.\n\n"
)

_PROMPT_welcome_call = _NO_AUTO_GREET + """\
You are Priya, a warm and professional Indian appointment booking assistant calling on behalf of {business_name}.
This is a welcome call for a new lead interested in {service_type}.

Your goal: confirm interest, understand the requirement, and book a callback or appointment.

After the customer responds to the greeting, continue naturally:
- Ask short, friendly questions one at a time.
- "Are you still looking for {service_type}?"
- "May I know your city or location?"
- "What time is comfortable for a callback or appointment?"

If busy: "Completely fine — when is a good time to call back?"
If interested: collect requirement, city, budget if relevant, and preferred time.
If not interested: "No worries at all. Have a great day!" — end call.
If asked to stop calling: "Noted, I'll update that right away. Sorry for the interruption!" — end call.
If asked if bot/AI: "I'm a virtual assistant for {business_name} — I can still help book you in!"

Style rules:
- Simple English with warm Indian spoken style. Sir/Madam naturally.
- Maximum 1–2 short sentences per turn.
- Never say "Certainly!" or filler openers.
- Match customer pace — if they say hold on, wait silently.
- If you do not know something, say the team will confirm and get back.
"""

_PROMPT_followup_call = _NO_AUTO_GREET + """\
You are Priya calling from {business_name} for a follow-up with {lead_name}.
This customer has already interacted with us before.

Do not treat this as a brand-new introduction.
After the customer responds, continue naturally — refer to their previous interest in {service_type}.
- "Is this still a good time to continue?"
- Confirm their requirement and agree on the next step.
- If they asked for a callback earlier, mention: "I'm calling back as you had requested."

Keep it short and polite. Do not repeat information they already know.
If busy: ask for a suitable callback time and note it.
"""

_PROMPT_inbound_call = """\
You are Priya, a helpful inbound call assistant for {business_name}.
The customer is calling us — greet politely and ask how you can help.

Answer questions about services, pricing, appointments, location, and working hours clearly.
If unsure, collect the details and say the team will confirm shortly.

Rules:
- Inbound support is available 24/7 unless business settings say otherwise.
- Do not apply outbound schedule rules to inbound calls.
- Be warm, clear, and helpful.
- If customer wants to book: collect name, preferred date/time, service.
- If customer wants a human: say the team will call back and note the request.
- Max 1–2 sentences per turn. No filler openers.
"""

_PROMPT_callback_call = _NO_AUTO_GREET + """\
You are Priya calling from {business_name}.
{lead_name} had requested a callback regarding {service_type}.

After they respond:
- "I'm calling back as you had requested — is this a good time?"
- Continue from the previous context if available.
- Confirm their requirement and next step.
- If still busy: "No problem — when can I call again?" — note the time.

Keep the call short and focused. Do not re-explain everything from scratch.
"""

_PROMPT_appointment_confirmation = _NO_AUTO_GREET + """\
You are Priya calling from {business_name} to confirm an appointment for {lead_name}.

After they respond:
- Confirm the scheduled date, time, and service.
- "Are you still available for your {service_type} appointment?"
- If confirmed: "Perfect — we look forward to seeing you. Have a great day!"
- If they want to reschedule: "No problem — what date and time works better for you?" — collect and note.

Keep this call very short — only 2–3 exchanges needed.
"""

_PROMPT_missed_call_retry = _NO_AUTO_GREET + """\
You are Priya calling from {business_name}.
We tried reaching {lead_name} earlier but could not connect.

After they respond:
- "We tried calling earlier regarding your {service_type} enquiry — hope you are doing well."
- "Is this a good time to speak for a minute?"
- If busy: "Not a problem — when is a good time for me to call back?"
- Do not sound frustrated or repeat that you called multiple times.

Stay polite and brief.
"""

_PROMPT_re_enquiry = _NO_AUTO_GREET + """\
You are Priya from {business_name}.
{lead_name} has enquired before and has now enquired again about {service_type}.

After they respond:
- "I noticed a new enquiry from you — welcome back!"
- "Are you looking for the same requirement as before, or something new?"
- If same: continue from previous context.
- If new: treat as a fresh opportunity while acknowledging their history with us.

Do not create a duplicate entry or duplicate conversation. Be aware of their history.
"""

_PROMPT_payment_followup = _NO_AUTO_GREET + """\
You are Priya from {business_name} following up with {lead_name} on a payment or next step.

After they respond:
- Be warm and professional — never pressure.
- "Just checking if you need any help completing the next step for {service_type}."
- If they have a question: answer or arrange a human callback.
- If they need more time: "Of course — we are here whenever you are ready."
- If they have paid or completed: "Wonderful — thank you so much!"

Keep the tone supportive, not chasing.
"""

_PROMPT_whatsapp_chat = """\
You are Priya, a WhatsApp chat assistant for {business_name}.
Reply in short, clear, friendly messages — this is a text chat, not a voice call.

Help with: FAQs, services, pricing, appointments, location, follow-up questions.
If user wants to book: collect name, service, preferred date/time.
If user asks for a call: ask for preferred time and create a callback request.
If human takeover is needed: "Let me connect you with our team — they will respond shortly."

Style: conversational, brief, no long paragraphs. Use plain language.
"""

_PROMPT_whatsapp_chat_prompt = """\
You are Priya, a WhatsApp chat assistant for {business_name}.
Reply in short, clear, friendly messages. This is WhatsApp chat, not a voice call.

Main goal: book a Google Meet demo or call appointment.
If the customer says booking, demo, appointment, meeting, or Google Meet: ask for their preferred date and time.
If the customer says call me, callback, or please call: politely acknowledge and ask for a preferred time if missing.
If the customer asks price: answer only from the knowledge base. If not available, say the team will confirm.
If the answer is not in the knowledge base, say the team will confirm and do not guess.

Style: natural WhatsApp sales assistant, brief, no long paragraphs, no phone-call wording.
"""

_PROMPT_whatsapp_chat_default = """\
You are a WhatsApp sales assistant for S Cube Digital Marketing / OutboundAI.

Your job is to chat with leads who enquired about AI Voice Agent, bulk voice calling, WhatsApp AI chat, appointment booking, follow-up calls, and demo booking.

Reply in short WhatsApp style.
Do not sound like a phone caller.
Do not give long paragraphs.
Ask only one question at a time.

Main goal:
Book a Google Meet demo.

If this is a new inbound chat with no useful history:
Say: Hi, thanks for contacting S Cube Digital Marketing. May I know your name and how can I assist you?

If customer says booking, demo, appointment, meeting, interested:
Say: Sure, I can help you book a demo. May I know your preferred date and time for a quick Google Meet demo?

If customer asks what service:
Say: We provide AI Voice Agent for lead follow-up, appointment booking, customer support, missed-call follow-up, and bulk outbound calling.

If customer says call me or callback:
Say: Sure, I will arrange a call for you. May I know your preferred time?

If customer asks price:
Say: AI voice calling starts from ₹5 per minute. Setup and monthly maintenance depend on your requirement. I can arrange a quick demo and explain the best package.

If answer is not available:
Say: Our team will confirm this during the demo.

Always keep replies short, professional, and friendly.
"""

# Registry of all prompt types
PROMPT_TYPES = [
    ("welcome_call",              "Welcome Call",              _PROMPT_welcome_call),
    ("followup_call",             "Follow-up Call",            _PROMPT_followup_call),
    ("inbound_call",              "Inbound Call",              _PROMPT_inbound_call),
    ("callback_call",             "Callback Call",             _PROMPT_callback_call),
    ("appointment_confirmation",  "Appointment Confirmation",  _PROMPT_appointment_confirmation),
    ("missed_call_retry",         "Missed Call Retry",         _PROMPT_missed_call_retry),
    ("re_enquiry",                "Re-enquiry",                _PROMPT_re_enquiry),
    ("payment_followup",          "Payment Follow-up",         _PROMPT_payment_followup),
    ("whatsapp_chat",             "WhatsApp Chat",             _PROMPT_whatsapp_chat_default),
]

# Fast lookup: type_key -> (label, default_prompt)
_PROMPT_DEFAULTS: dict = {pt: (lbl, dflt) for pt, lbl, dflt in PROMPT_TYPES}


def get_default_prompt(prompt_type: str) -> str:
    """Return the built-in default prompt text for a given type, or welcome_call fallback."""
    entry = _PROMPT_DEFAULTS.get(prompt_type)
    return entry[1] if entry else _PROMPT_DEFAULTS["welcome_call"][1]


def get_prompt_label(prompt_type: str) -> str:
    entry = _PROMPT_DEFAULTS.get(prompt_type)
    return entry[0] if entry else prompt_type.replace("_", " ").title()


def build_knowledge_context(kb: dict, max_chars: int = 4000) -> str:
    """
    Convert a Knowledge Base dict into a clean text block suitable for injection
    into Gemini prompts.  Truncated to max_chars to stay within context limits.
    Returns an empty string if the KB is empty / has no useful content.
    """
    if not kb or not isinstance(kb, dict):
        return ""

    lines: list = []

    # Company
    cp = kb.get("company_profile") or {}
    if cp.get("business_name"):
        lines.append(f"[COMPANY KNOWLEDGE BASE]")
        lines.append(f"Business: {cp.get('business_name','')}")
        if cp.get("industry_type"):
            lines.append(f"Industry: {cp['industry_type']}")
        if cp.get("short_description"):
            lines.append(f"Description: {cp['short_description']}")
        if cp.get("about_us"):
            lines.append(f"About: {cp['about_us']}")
        if cp.get("website"):
            lines.append(f"Website: {cp['website']}")
        if cp.get("email"):
            lines.append(f"Email: {cp['email']}")

    # Contact
    cd = kb.get("contact_details") or {}
    contact_bits = []
    for f in ("primary_phone", "support_phone", "whatsapp_number", "email", "address", "city", "state"):
        if cd.get(f):
            contact_bits.append(f"{f.replace('_', ' ').title()}: {cd[f]}")
    if cd.get("google_maps_link"):
        contact_bits.append(f"Maps: {cd['google_maps_link']}")
    if contact_bits:
        lines.append("")
        lines.append("[CONTACT]")
        lines.extend(contact_bits)

    # Working hours
    wh = kb.get("working_hours") or {}
    if wh.get("opening_time") or wh.get("opening_days"):
        lines.append("")
        lines.append("[WORKING HOURS]")
        days = wh.get("opening_days")
        if isinstance(days, list):
            days = ", ".join(days)
        if days:
            lines.append(f"Days: {days}")
        if wh.get("opening_time") and wh.get("closing_time"):
            lines.append(f"Hours: {wh['opening_time']} – {wh['closing_time']}")
        if wh.get("timezone"):
            lines.append(f"Timezone: {wh['timezone']}")
        if wh.get("holiday_notes"):
            lines.append(f"Holidays: {wh['holiday_notes']}")
        if wh.get("emergency_support_available"):
            lines.append("Emergency support: Available")

    # Services
    services = [s for s in (kb.get("services") or []) if s.get("active", True) and s.get("name")]
    if services:
        lines.append("")
        lines.append("[SERVICES / PRODUCTS]")
        for s in services[:10]:
            parts = [s["name"]]
            if s.get("category"):
                parts.append(f"({s['category']})")
            if s.get("description"):
                parts.append(f"— {s['description']}")
            price_parts = []
            if s.get("price_from"):
                price_parts.append(str(s["price_from"]))
            if s.get("price_to"):
                price_parts.append(str(s["price_to"]))
            if price_parts:
                parts.append(f"Price: {' – '.join(price_parts)}")
            if s.get("duration"):
                parts.append(f"Duration: {s['duration']}")
            lines.append("• " + " ".join(parts))

    # Packages
    packages = [p for p in (kb.get("packages") or []) if p.get("active", True) and p.get("package_name")]
    if packages:
        lines.append("")
        lines.append("[PACKAGES]")
        for p in packages[:6]:
            parts = [p["package_name"]]
            if p.get("price"):
                parts.append(f"— ₹{p['price']}")
            if p.get("description"):
                parts.append(f"| {p['description']}")
            if p.get("validity"):
                parts.append(f"| Valid: {p['validity']}")
            lines.append("• " + " ".join(parts))

    # FAQs
    faqs = [f for f in (kb.get("faqs") or []) if f.get("active", True) and f.get("question")]
    if faqs:
        lines.append("")
        lines.append("[FAQs]")
        for faq in faqs[:15]:
            lines.append(f"Q: {faq['question']}")
            if faq.get("answer"):
                lines.append(f"A: {faq['answer']}")

    # Policies (non-empty)
    pol = kb.get("policies") or {}
    pol_parts = []
    for k, label in [
        ("cancellation_policy", "Cancellation"),
        ("refund_policy", "Refund"),
        ("appointment_policy", "Appointment"),
        ("payment_policy", "Payment"),
        ("terms_notes", "Terms"),
    ]:
        if pol.get(k):
            pol_parts.append(f"{label}: {pol[k]}")
    if pol_parts:
        lines.append("")
        lines.append("[POLICIES]")
        lines.extend(pol_parts)

    # Appointment rules
    ar = kb.get("appointment_rules") or {}
    ar_parts = []
    if ar.get("appointment_required"):
        ar_parts.append("Appointment required: Yes")
    if ar.get("allow_same_day_booking"):
        ar_parts.append("Same-day booking: Allowed")
    if ar.get("appointment_duration_minutes"):
        ar_parts.append(f"Duration: {ar['appointment_duration_minutes']} min")
    if ar.get("default_visit_type"):
        ar_parts.append(f"Default visit: {ar['default_visit_type'].replace('_', ' ')}")
    if ar.get("confirmation_required"):
        ar_parts.append("Confirmation: Required")
    if ar_parts:
        lines.append("")
        lines.append("[APPOINTMENT RULES]")
        lines.extend(ar_parts)

    # Human transfer
    tr = kb.get("transfer_rules") or {}
    if tr.get("transfer_enabled") and tr.get("transfer_number"):
        lines.append("")
        lines.append("[HUMAN TRANSFER]")
        lines.append(f"Transfer number: {tr['transfer_number']}")
        if tr.get("transfer_conditions"):
            lines.append(f"Conditions: {tr['transfer_conditions']}")
        if tr.get("working_hours_only"):
            lines.append("Available: Working hours only")

    if not lines:
        return ""

    # KB grounding instruction
    lines.append("")
    lines.append(
        "IMPORTANT: Answer all business-related questions strictly using the company knowledge base above. "
        "If information is not available in the knowledge base, say our team will confirm and get back."
    )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[knowledge base truncated]"
    return text


def get_kb_prompt_prefix(kb: dict, max_chars: int = 4000) -> str:
    """Return KB context block ready to prepend to any call-type prompt."""
    ctx = build_knowledge_context(kb, max_chars=max_chars)
    return (ctx + "\n\n") if ctx else ""


def build_prompt_for_type(
    prompt_type: str,
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
    saved_text: str = None,
    kb: dict = None,
    customer_name: str = None,
    company_name: str = None,
    requirement: str = "",
    source: str = "",
) -> str:
    """Build final prompt: use saved_text if provided, else built-in default for type.
    If kb is provided, prepend the KB context block."""
    template = saved_text if saved_text else get_default_prompt(prompt_type)
    customer_name = customer_name if customer_name is not None else lead_name
    company_name = company_name or business_name
    values = {
        "name": customer_name,
        "customer_name": customer_name,
        "lead_name": lead_name,
        "business_name": business_name,
        "company_name": company_name,
        "service_type": service_type,
        "requirement": requirement or service_type,
        "source": source,
        "call_type": prompt_type,
    }
    body = template.format_map(_SafePromptFields(values))
    if kb:
        prefix = get_kb_prompt_prefix(kb)
        if prefix:
            return prefix + body
    return body
