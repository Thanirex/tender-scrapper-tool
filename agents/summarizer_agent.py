import os
import re
import time
# from groq import Groq
import google.generativeai as genai

_MAX_CHARS_DEFAULT = 500_000
# _MODEL = "llama-3.1-8b-instant"   # Groq model
_MODEL = "gemini-2.5-flash-lite"          # Gemini model
# _TEMPERATURE = 0.1                 # Groq temperature (Gemini uses generation_config instead)
_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF_S = 65


class SummarizerAgent:

    _KNOWN_KEYS = {
        "REFERENCE_NO", "BID_TITLE", "PUBLISHED_ON", "PRE_BID_MEETING", "DEADLINE",
        "INVITING_AUTHORITY", "CONTACT_EMAIL", "FUNDING_AGENCY", "COUNTRY", "DOMAIN",
        "SUB_DOMAIN", "TMI_SERVICE_LINE", "PROJECT_VALUE", "ELIGIBILITY_FLAG",
        "ELIGIBILITY_DETAILS", "ACCESS_REQUIREMENTS", "DEPENDENCIES", "CONSORTIUM",
        "SCOPE_OF_WORK", "DELIVERY_PERIOD", "EMDS", "PAYMENT_TERMS", "PRICING_CONDITIONS",
        "SELECTION_CRITERIA", "SCOPE_CLARITY", "SCOPE_QUANTUM", "CLARIFICATIONS", "PENALTIES",
    }

    def __init__(self):
        # ── Groq client (commented out) ──────────────────────────────────────
        # api_key = os.getenv("GROQ_API_KEY")
        # if not api_key or api_key == "your_groq_api_key_here":
        #     print("⚠️ WARNING: Invalid or missing GROQ_API_KEY in .env. Falling back to dummy mode.")
        #     self.client = None
        # else:
        #     self.client = Groq(api_key=api_key)

        # ── Gemini client ─────────────────────────────────────────────────────
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your_gemini_api_key_here":
            print("⚠️ WARNING: Invalid or missing GEMINI_API_KEY in .env. Falling back to dummy mode.")
            self.client = None
        else:
            genai.configure(api_key=api_key)
            self.client = True  # configured globally via genai.configure

    # ── Public methods ────────────────────────────────────────────────────────

    def summarize(self, text, log_callback=None, max_chars=_MAX_CHARS_DEFAULT):
        log = self._make_logger(log_callback)

        if not self._has_content(text):
            return "No meaningful content found to summarize."
        if not self.client:
            return "(Gemini Key Not Setup) - Placeholder Summary."

        snippet = text[:max_chars]
        log(f"🤖 [Summarizer] Reading {len(snippet):,} chars across page + documents...")

        system = (
            "You are a precise procurement analyst. "
            "You read tender documents carefully and extract specific factual information. "
            "You never invent information — if something is not in the provided content you say 'Not specified'. "
            "You always check ALL sections of the provided content, including tables, footers, and appendices."
        )
        prompt = self._build_summarize_prompt(snippet)
        raw = self._call_api(system, prompt, log, error_return="Error generating summary.")
        return raw

    def summarize_level1(self, text, log_callback=None, max_chars=_MAX_CHARS_DEFAULT):
        """Extract all Level 1 template fields from tender content. Returns a dict."""
        log = self._make_logger(log_callback)

        if not self._has_content(text):
            return {}
        if not self.client:
            return {"BID_TITLE": "(Gemini Key Not Setup)"}

        snippet = text[:max_chars]
        log(f"🤖 [Summarizer] Extracting Level 1 fields from {len(snippet):,} chars...")

        system = (
            "You are a precise procurement analyst for TMI. "
            "Extract tender fields exactly as labeled. Never invent data. "
            "Output ONLY the labeled field lines — no headers, no explanations, no blank lines."
        )
        prompt = self._build_level1_prompt(snippet)
        raw = self._call_api(system, prompt, log, error_return=None)
        if raw is None:
            return {}
        return self._parse_level1_fields(raw)

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_logger(log_callback):
        """Return a unified log function that routes to the callback or stdout."""
        return log_callback if log_callback else print

    @staticmethod
    def _has_content(text) -> bool:
        return bool(text and len(text.strip()) >= 20)

    def _call_api(self, system: str, prompt: str, log, error_return):
        """
        Call Gemini with retry on rate-limit (HTTP 429 / RESOURCE_EXHAUSTED).
        Returns the stripped response string, or error_return on failure.
        """
        # ── Groq call (commented out) ─────────────────────────────────────────
        # messages = [
        #     {"role": "system", "content": system},
        #     {"role": "user",   "content": prompt},
        # ]
        # response = self.client.chat.completions.create(
        #     messages=messages,
        #     model=_MODEL,
        #     temperature=_TEMPERATURE,
        # )
        # return response.choices[0].message.content.strip()

        # ── Gemini call ───────────────────────────────────────────────────────
        model = genai.GenerativeModel(
            model_name=_MODEL,
            system_instruction=system,
        )
        for attempt in range(_MAX_RETRIES):
            try:
                response = model.generate_content(prompt)
                return response.text.strip()
            except Exception as e:
                err = str(e)
                is_rate_limit = (
                    "429" in err or "RESOURCE_EXHAUSTED" in err or
                    "quota" in err.lower() or "rate" in err.lower()
                )
                if is_rate_limit and attempt < _MAX_RETRIES - 1:
                    wait_s = _RATE_LIMIT_BACKOFF_S * (attempt + 1)
                    log(f"⏳ Gemini rate limit hit — waiting {wait_s}s before retry ({attempt + 2}/{_MAX_RETRIES})...")
                    time.sleep(wait_s)
                    continue
                log(f"❌ Gemini Error: {e}")
                return error_return
        return error_return

    @staticmethod
    def _build_summarize_prompt(snippet: str) -> str:
        return f"""You are a senior procurement analyst specializing in e-learning, digital education, capacity building, and training technology tenders for international development organizations (UN agencies, NGOs, governments).

You have been given content extracted from a UN Global Marketplace (UNGM) procurement notice. The content includes labeled sections:

  === VERIFIED FIELDS (scraped directly from UNGM — treat as ground truth) ===
  These fields were scraped directly from the UNGM structured HTML. They are authoritative.
  RULE: If "Deadline on" appears here, copy it EXACTLY as your deadline answer — do not alter the date, time, or timezone. Do not use any other date from the documents unless there is a clear contradiction, in which case flag [MISMATCH].

  === UNGM NOTICE PAGE TEXT ===
  Full visible text of the UNGM notice page.

  === ATTACHED DOCUMENT: filename ===
  Text extracted from a downloaded tender document (PDF, Word, Excel).

Read ALL sections carefully before answering. Cross-reference across documents — budget/quantum is often only inside attached PDF/Word documents, not on the notice page.

---

CONFIDENCE MARKERS — you MUST tag every field with exactly one of:
  [VERIFIED]   — value taken directly from the VERIFIED FIELDS section (ground truth)
  [EXTRACTED]  — value found in the notice page text or attached documents
  [NOT_FOUND]  — genuinely absent from all provided content
  [MISMATCH: HIGH PRIORITY ERROR] — a VERIFIED field value contradicts what is stated in the documents; quote both values

Place the marker at the START of the field value, on the same line as the field label.

---

Extract the following 5 fields. Be specific and detailed.

1. Name of bid / Title:
Full official name or title of this tender. Check VERIFIED FIELDS first, then the page text.

2. Deadline:
STEP 1 — look for "Deadline on" in the VERIFIED FIELDS section and use that value exactly.
STEP 2 — if not in VERIFIED FIELDS, search attached documents (tables, headers, footers).
STEP 3 — if found in both and they differ, output [MISMATCH: HIGH PRIORITY ERROR] and quote both.
Include date, time, and timezone exactly as written.

3. Scope of Work:
Exactly what the vendor must deliver. Include: type of service/product, target audience, geography, duration, key deliverables, technical specs. Be thorough — pull from all sections.

4. Eligibility criteria:
Who can apply. Include: entity type (NGO, firm, consultant), minimum experience, prior UN/UNGM registration requirement, certifications, language requirements, exclusions. Search all attached documents — eligibility tables are often inside RFP documents, not the notice page.

5. Quantum:
Contract value / total budget. Search ALL content — this appears as "budget", "contract value", "ceiling price", "estimated cost", "financial envelope", "maximum budget", or inside financial tables in attached documents. State currency, amount, and whether it is a ceiling or estimate. If a range, give both ends.

---

TENDER CONTENT:
{snippet}

---

OUTPUT (use exactly the numbered format; place confidence marker immediately after the colon on each field line):

1. Name of bid / Title: [MARKER] value here
2. Deadline: [MARKER] value here
3. Scope of Work: [MARKER] value here
4. Eligibility criteria: [MARKER] value here
5. Quantum: [MARKER] value here"""

    @staticmethod
    def _build_level1_prompt(snippet: str) -> str:
        return f"""You are a senior procurement analyst for TMI (Training & Management International), specialising in e-learning, LMS, capacity building, and training technology tenders.

Read EVERY section of the tender content below — VERIFIED FIELDS, notice page text, and ALL attached documents (including headers, footers, cover pages, contact sections, and annexes) — then extract the fields below.

OUTPUT FORMAT — output ONLY these labeled lines, one per line, nothing else:
FIELD_NAME: value

EXTRACTION RULES:
- VERIFIED FIELDS section is ground truth; use those values exactly.
- Search the FULL text — fields like email addresses and reference codes often appear in page headers, footers, "Contact" or "How to Participate" sections, NOT in the main body.
- Write "Not specified" ONLY when a field is genuinely absent from ALL content — not just from the summary paragraph.
- Do NOT guess. Do NOT infer from context. Extract verbatim where possible.
- TMI_SERVICE_LINE: exactly one of C&K / HR&KM / Both
- ELIGIBILITY_FLAG: Yes / Conditional / Not specified
- CONSORTIUM: Allowed / Not Allowed / Permitted with conditions / Not specified
- SCOPE_CLARITY: Yes / Partially / No
- Keep each value on ONE line (no line breaks inside a value).
- SCOPE_OF_WORK, ELIGIBILITY_DETAILS, and ACCESS_REQUIREMENTS may be up to 500 chars.

REQUIRED FIELDS:
REFERENCE_NO: official tender reference or RFP number (e.g. IFAD/2026/006/RFP). Look for patterns like ORG/YEAR/NNN/TYPE or any alphanumeric code labelled "reference", "RFP no.", "tender no.", "notice ref."
BID_TITLE: full official title of the tender — do not abbreviate
PUBLISHED_ON: publication/notice date (e.g. 16-Apr-2026)
PRE_BID_MEETING: pre-bid or pre-proposal meeting date/time, or Not specified
DEADLINE: submission deadline with date, time, and timezone exactly as written
INVITING_AUTHORITY: full official name of the inviting organisation — do NOT abbreviate
CONTACT_EMAIL: procurement contact email address. Scan ALL sections for @ symbols, "Contact:", "Correspondence", "Enquiries" headings. If multiple emails, list them separated by semicolons.
FUNDING_AGENCY: donor or funding agency (if same as inviting authority, repeat it)
COUNTRY: country or region of project assignment
DOMAIN: broad domain (e.g. IT, Education, Training, HR, Health)
SUB_DOMAIN: specific technology or focus (e.g. LMS, Moodle, e-learning, capacity building)
TMI_SERVICE_LINE: C&K or HR&KM or Both
PROJECT_VALUE: estimated budget or contract value with currency and ceiling/estimate flag
ELIGIBILITY_FLAG: Yes / Conditional / Not specified
ELIGIBILITY_DETAILS: ALL eligibility criteria — entity type, years of experience, certifications, prior UN registration, any exclusions; include platform/registration prerequisites
ACCESS_REQUIREMENTS: registration or platform requirements to access bid documents or submit a bid (e.g. must be registered on UNGM, must be IFAD-registered vendor, must use In-tend e-tendering portal). Look in "How to access", "Participation", "Registration" sections.
DEPENDENCIES: technical, regulatory, or compliance prerequisites for delivery (infrastructure, GDPR, ISO standards, data residency, etc.)
CONSORTIUM: Allowed / Not Allowed / Permitted with conditions / Not specified
SCOPE_OF_WORK: COMPLETE and detailed scope — list ALL workstreams, deliverables, services, and technical requirements found across every section. Include: type of service/product, customisation, hosting, maintenance, support, training, geography, and duration.
DELIVERY_PERIOD: contract duration or delivery timeline
EMDS: earnest money deposit / bid bond amount and type, or Not required
PAYMENT_TERMS: payment schedule or terms (e.g. milestone-based, monthly, advance payment)
PRICING_CONDITIONS: specific pricing requirements — VAT treatment (net of / inclusive), fixed vs variable price, tax exemption status, currency restrictions. Look in commercial/financial conditions sections.
SELECTION_CRITERIA: evaluation methodology (e.g. QCBS, L1, MEAT, RFP scoring breakdown)
SCOPE_CLARITY: Yes / Partially / No — how clearly the scope is defined
SCOPE_QUANTUM: brief characterisation of scale and complexity (e.g. "enterprise LMS + 600+ plugins, 5-year contract")
CLARIFICATIONS: deadline and method for submitting clarifications/queries
PENALTIES: liquidated damages or penalties for delay or non-performance

TENDER CONTENT:
{snippet}"""

    def _parse_level1_fields(self, raw: str) -> dict:
        fields = {}
        for line in raw.splitlines():
            if ": " not in line:
                continue
            key, _, val = line.partition(": ")
            key = key.strip()
            if key not in self._KNOWN_KEYS:
                continue
            val = re.sub(r'^\[(?:VERIFIED|EXTRACTED|NOT_FOUND|V|MISMATCH[^\]]*)\]\s*', '', val).strip()
            fields[key] = val
        return fields
