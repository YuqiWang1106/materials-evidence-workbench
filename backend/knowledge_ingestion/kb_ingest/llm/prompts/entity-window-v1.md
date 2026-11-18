
一次读取 source，在同一个 JSON 中完整返回 entities 和 attributes。
先确定实体和 ID，再在同一份回答内写它们的属性（实体之间的联系写在属性的 refs 中）。不要只返回实体清单。
known_entities 仅供名称统一；known_formulations 是程序给出的前文配方，可直接用其 local_id 写本窗口有依据的属性。
known_formulations 的 ID 是保留 ID，不得给新实体使用，也不要在 entities 中重新声明这些 ID。
本窗口再次明确提到已有配方时，可以用新的唯一 ID 列出它及当前片段，名称沿用 known_entities。
所有引用只用本次 entities 或 known_formulations 中的 ID；不得虚构 known 标记或外部实体。
无论样品多少，都在同一份回答中提取有原文依据的内容；不要省略后面的样品。
以下两部分都是本次回答的要求：
