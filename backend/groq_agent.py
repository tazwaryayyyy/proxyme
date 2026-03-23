import os
from groq import AsyncGroq

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))


class GroqAgent:
    async def generate_response(self, transcript, config, matched_rule):
        user_name = config.get("name", "the user")
        role = config.get("role", "professional")
        context = config.get("context", "a business meeting")
        tone = config.get("tone", "professional and concise")
        rule_note = f"\nApproved topic: {matched_rule}" if matched_rule else ""

        try:
            response = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=200,
                messages=[
                    {
                        "role": "system",
                        "content": f"""You are an AI meeting assistant speaking on behalf of {user_name}, a {role}.
Context: {context}
Tone: {tone}{rule_note}
Generate a SHORT natural spoken response (1-3 sentences max).
Sound human and natural, not robotic. Be direct and confident.
Do NOT add disclaimers or explain that you are an AI.
Write in first person as if {user_name} is speaking."""
                    },
                    {
                        "role": "user",
                        "content": f'Someone in the meeting said or asked: "{transcript}"\n\nGenerate {user_name}\'s response:'
                    }
                ]
            )
            return response.choices[0].message.content.strip()
        except Exception:
            return "I'll need a moment to think about that."

    async def generate_summary(self, transcript, responses):
        full = "\n".join(transcript)
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=500,
            messages=[
                {
                    "role": "system",
                    "content": "You summarize meeting transcripts concisely. Output bullet points of key topics discussed and any action items."
                },
                {
                    "role": "user",
                    "content": f"Summarize this meeting transcript:\n{full}"
                }
            ]
        )
        return response.choices[0].message.content.strip()
