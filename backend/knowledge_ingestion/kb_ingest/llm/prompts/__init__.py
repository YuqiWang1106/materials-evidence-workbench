"""读取随网关发布的版本化提示词，保留模型收到的 UTF-8 原文。"""

from importlib.resources import files


def load_prompt(name: str) -> str:
    return files(__name__).joinpath(f"{name}.md").read_bytes().decode("utf-8")
