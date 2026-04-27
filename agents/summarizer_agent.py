import os
import time
from groq import Groq


# Free-tier TPM cap is 12,000 tokens/min.
# Prompt overhead ≈ 2,200 tokens → content budget ≈ 9,800 tokens ≈ 28,000 chars.
_MAX_CHARS_DEFAULT = 28000


class SummarizerAgent:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key or api_key == "your_groq_api_key_here":
            print("⚠️ WARNING: Invalid or missing GROQ_API_KEY in .env. Falling back to dummy mode.")
            self.client = None
        else:
            self.client = Groq(api_key=api_key)

    def summarize(self, text, log_callback=None, max_chars=_MAX_CHARS_DEFAULT):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        if not text or len(text.strip()) < 20:
            return "No meaningful content found to summarize."

        if not self.client:
            return "(Groq Key Not Setup) - Placeholder Summary."

        text_snippet = text[:max_chars]

        prompt = f"""You are a senior procurement analyst specializing in e-learning, digital education, capacity building, and training technology tenders for international development organizations (UN agencies, NGOs, governments).

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
{text_snippet}

---

OUTPUT (use exactly the numbered format; place confidence marker immediately after the colon on each field line):

1. Name of bid / Title: [MARKER] value here
2. Deadline: [MARKER] value here
3. Scope of Work: [MARKER] value here
4. Eligibility criteria: [MARKER] value here
5. Quantum: [MARKER] value here"""

        log(f"🤖 [Summarizer] Reading {len(text_snippet):,} chars across page + documents...")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise procurement analyst. "
                    "You read tender documents carefully and extract specific factual information. "
                    "You never invent information — if something is not in the provided content you say 'Not specified'. "
                    "You always check ALL sections of the provided content, including tables, footers, and appendices."
                )
            },
            {"role": "user", "content": prompt}
        ]

        # Retry up to 3 times on rate-limit (413/429); back off 65 s to let the TPM window reset.
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    messages=messages,
                    model="llama-3.3-70b-versatile",
                    temperature=0.1,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                err_str = str(e)
                is_rate_limit = ("413" in err_str or "429" in err_str or
                                 "rate_limit" in err_str or "tokens per minute" in err_str.lower())
                if is_rate_limit and attempt < 2:
                    wait_s = 65 * (attempt + 1)
                    log(f"⏳ Groq rate limit hit — waiting {wait_s}s before retry ({attempt + 2}/3)...")
                    time.sleep(wait_s)
                    continue
                log(f"❌ Groq Error: {e}")
                return "Error generating summary."
