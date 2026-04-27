import os
from groq import Groq

class SummarizerAgent:
    def __init__(self):
        # We enforce reading from the env variable
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key or api_key == "your_groq_api_key_here":
            print("⚠️ WARNING: Invalid or missing GROQ_API_KEY in .env. Falling back to dummy mode.")
            self.client = None
        else:
            self.client = Groq(api_key=api_key)

    def summarize(self, text, log_callback=None, max_chars=4000):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        if not text or len(text.strip()) < 20:
            return "No meaningful content found to summarize."

        if not self.client:
            return "(Groq Key Not Setup) - Placeholder Summary: This tender focuses on capacity building and e-learning resources."

        text_snippet = text[:max_chars]
        
        prompt = f"""You are a professional tender and RFP analyst.
Please read the following scraped text from a tender website.
Extract and summarize the information exactly in this format. If a field is not mentioned, write "Not specified".

1. Name of bid/ Title:
2. Deadline:
3. Scope of Work:
4. Eligibility criteria:
5. Quantum:

TENDER DESCRIPTION:
{text_snippet}

SUMMARY:"""

        log(f"🤖 [Summarizer] Calling Groq LLM...")
        try:
            chat_completion = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You are a concise, highly accurate procurement analyst."},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.1-8b-instant",  # Blazing fast Llama-3.1 model on Groq
                temperature=0.2,
            )
            return chat_completion.choices[0].message.content.strip()
        except Exception as e:
            log(f"❌ Groq Error: {e}")
            return "Error generating summary."
