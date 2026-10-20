import os
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3001/v1",
    api_key=os.environ.get("FREELLMAPI_KEY",
        "freellmapi-9648d07db1e66eaca78226512eee95efafc9e9092afd5769"),
)
MODEL = "auto"   # use the same model name you used in llm.py

history = [{"role": "system", "content": "You are a helpful assistant."}]
print("Chat started. Type 'exit' to quit, 'clear' to start over.\n")

while True:
    try:
        q = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        break
    if not q:
        continue
    if q.lower() == "exit":
        break
    if q.lower() == "clear":
        history = history[:1]
        print("(history cleared)\n")
        continue

    history.append({"role": "user", "content": q})
    print("AI: ", end="", flush=True)
    reply = ""
    try:
        stream = client.chat.completions.create(model=MODEL, messages=history, stream=True)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                part = chunk.choices[0].delta.content
                reply += part
                print(part, end="", flush=True)
    except Exception as e:
        print(f"[error: {e}]")
        history.pop()
        continue
    print("\n")
    history.append({"role": "assistant", "content": reply})
