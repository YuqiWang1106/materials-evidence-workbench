import re

def _text(value):
    return re.sub(r"\s+", "", str(value)).replace("−", "-").replace("–", "-")
