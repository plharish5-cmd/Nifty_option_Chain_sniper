import sys
from llm import ask

path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()

data = "".join(lines[:150])  # first 150 rows, to stay within free-tier limits

system = "You are an options analyst. Be concise and use numbers from the data."
prompt = f"""Here is an option chain (CSV):

{data}

1. Strike with the highest call OI and highest put OI (likely resistance / support)
2. Put-call ratio by OI, if computable
3. Any notable OI build-up or unusual activity
4. A 3-line overall view"""

print(ask(prompt, system=system, show_route=True))
