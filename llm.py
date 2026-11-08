import os
from dotenv import load_dotenv, find_dotenv
from google import genai
from google.genai import types, errors

load_dotenv(find_dotenv())

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def ask(prompt, system="You are a helpful assistant.", show_route=False):
    for model_name in ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                ),
            )
            if show_route:
                print(f"Served by: {model_name}")
            return response.text
        except errors.ServerError:
            continue
    raise RuntimeError("All fallback Gemini models returned 503 UNAVAILABLE.")
