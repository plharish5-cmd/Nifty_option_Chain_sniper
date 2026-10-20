import os
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3001/v1",
    api_key=os.environ["FREELLMAPI_KEY"],
)

def ask(prompt, system="You are a helpful assistant.", show_route=False):
    raw = client.chat.completions.with_raw_response.create(
        model="auto",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    if show_route:
        print("Served by:", raw.headers.get("X-Routed-Via"))
    return raw.parse().choices[0].message.content
