import os
from google import genai
from tenacity import retry, stop_after_attempt, wait_exponential

client = genai.Client()

# Automatically retry up to 5 times if a server-side 503 error drops the request
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=15))
def run_test():
    print("Attempting to connect to gemini-3.8-flash...")
    response = client.models.generate_content(
        model='gemini-3.8-flash',
        contents='Confirm connection: Reply with "Option chain workspace ready!"',
    )
    return response

try:
    response = run_test()
    print("\n[SUCCESS] Response from AI:")
    print(response.text)
except Exception as e:
    print(f"\n[ERROR] Connection failed after multiple retries: {e}")
