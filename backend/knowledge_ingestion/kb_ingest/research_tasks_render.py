"""User-readable deterministic task text; identifiers stay in structured support."""

from .core.serialization import canonical
from .research_chat_contracts import ResearchError
from .research_observations import reference_values

STATES = {"satisfied": "满足", "not_satisfied": "不满足", "unknown": "未知"}
REASONS = {
    "not_read_or_not_reported": "已读记录中未找到这项依据",
    "test_unconfirmed": "测试归属未确认",
    "test_owner_mismatch": "条件属于另一测试",
    "not_observed_fact": "内容不属于可用的观测或来源报告",
    "evidence_or_owner_missing": "原文依据或归属缺失",
    "non_scalar_measurement": "数值不是可直接比较的单值",
    "typed_value_unverified": "数值解析未确认",
    "target_unit_missing": "目标单位缺失",
    "unit_missing": "来源单位缺失",
    "unit_mismatch": "单位不一致",
    "basis_missing": "配方基准缺失",
    "basis_mismatch": "基准不一致",
    "conflicting_values": "同一测试有多个不同数值",
}


def render_target(target):
    return "用户目标（原始输入，非实测）：" + "；".join(f["text"] for f in target["fragments"]) + "。"


def render_task(result, registry, *, presentation_version=None):
    if presentation_version is not None:
        if presentation_version != "plan-v2" or result["kind"] != "research_plan":
            raise ResearchError("stale_context")
        return _render_plan_v2(result, registry)
    kind = result["kind"]
    rows = result["inputs"]["candidates"]

    def label(ref):
        name = registry._label(ref, "未命名样品")
        return (
            name
            + "（来源："
            + str(ref["source_id"])
            + "；"
            + ("所选测试" if ref.get("test_ref") else "样品层，测试未确认")
            + "）"
        )

    if kind == "constraint_evaluation":
        coverage = result["coverage"]
        target = result["inputs"]["target"]
        text = [render_target(target)]
        if coverage["exhaustive"]:
            text.append("已检查本次筛选条件匹配的全部候选；这不表示已检查所选资料中的所有配方。")
        else:
            text.append("以下结论仅限已检查候选；尚不能判断其他候选，也不能据此认定无解。")
        constraints = {c["constraint_id"]: f["text"] for c, f in zip(target["constraints"], target["fragments"])}
        for candidate in result["candidates"]:
            text.append(label(candidate["ref"]) + "：" + STATES[candidate["status"]] + "。")
            for check in candidate["checks"]:
                text.append(
                    constraints[check["constraint_id"]]
                    + "："
                    + STATES[check["status"]]
                    + ("（" + REASONS.get(check["reason"], "依据不足，需核对") + "）" if check["reason"] else "")
                    + "。"
                )
        text.append(
            "以上是依据已读记录与用户目标的程序核对，不是科学验证、性能排名或唯一最优结论。下一步需补齐未知项并核对原文与测试条件。"
        )
        return "\n".join(text)
    if kind == "performance_reference":
        text = [
            "性能参考：目前没有可用的预测模型，不能给出数值预测。以下仅为已读实测或来源报告的参考，不代表新配方性能。"
        ]
        for row in rows:
            values = reference_values(row)
            if values:
                text.append(
                    label(row["ref"])
                    + "："
                    + "；".join(
                        registry._field(v)
                        + "为 "
                        + registry._value_text(v)
                        + (
                            "（来源报告测量）"
                            if v["statement_kind"] == "source_reported_measurement"
                            else "（" + ("实测记录" if v["statement_kind"] == "measured" else "来源报告") + "）"
                        )
                        for v in values
                    )
                    + "。"
                )
            else:
                text.append(
                    label(row["ref"])
                    + "：当前未读到类型、单位与归属均可核验的性能观测；不等于原资料没有结果，可查询原记录。"
                )
        for pair in result["comparisons"]:
            text.append(registry._comparison_text({"pair": pair}))
        if not result["comparisons"]:
            text.append("可比性尚未核对，不能进行性能差值、排名或外推。")
        text.append("测试方法与测试条件须按各自测试核对；未报告或未核对的条件仍为未知，不能视为条件相同。")
        text.append("数值预测尚需训练样本、明确目标定义、条件覆盖、独立验证和适用范围的依据；当前不宣称预测已就绪。")
        return "\n".join(text)
    text = [render_target(result["inputs"]["target"]), "研发方案草案（未验证；未提交任何实验、训练或科学任务）。"]
    text.append("候选：" + "；".join(label(r) for r in result["candidate_refs"]) + "。")
    if result["parameters"]:
        text.append("原文制备参数（按各自过程与步骤保留，不能直接视为新实验设置）：")
        for p in result["parameters"]:
            text.append(
                registry._field(p["value"]) + "为 " + registry._value_text(p["value"]) + "（过程与步骤归属见依据）。"
            )
    else:
        text.append("当前未读到带完整过程归属的制备参数。")
    text.append("过程步骤顺序待确认；新实验的温度、时长、混合比例分别待确认，不能从其他步骤补齐。")
    text.append(
        "下一步建议：核对各步骤与参数；选择对照并确定复测安排；确认测试方法和条件；以用户目标作为判断门槛逐项核对观测。以上仅为定性建议。"
    )
    return "\n".join(text)


def _render_plan_v2(result, registry):
    """Present verified plan inputs; no new classification or experimental settings."""
    rows = result["inputs"]["candidates"]
    target = result["inputs"]["target"]

    def candidate_name(row):
        ref = row["ref"]
        return (
            (row.get("label") or "未命名样品")
            + "（来源："
            + ref["source_id"]
            + "；样品："
            + ref["sample_ref"]
            + "；测试："
            + (ref.get("test_ref") or "未确认")
            + "）"
        )

    def value_text(value):
        return registry._field(value) + "为 " + registry._value_text(value)

    text = ["研发方案草案（未验证；未提交任何实验、训练或科学任务）。", "一、目标与候选", render_target(target)]
    text.extend(candidate_name(row) for row in rows)
    text.append("这些是本次选定的来源候选，不表示已完成条件筛选、性能排名或唯一最优证明。")
    text.append("二、来源依据（既有记录，不是新配方预测）")
    for row in rows:
        text.append(candidate_name(row))
        values = reference_values(row)
        if values:
            text.append(
                "；".join(
                    value_text(v)
                    + (
                        "（来源报告测量）"
                        if v["statement_kind"] == "source_reported_measurement"
                        else "（实测记录）"
                        if v["statement_kind"] == "measured"
                        else "（来源报告）"
                    )
                    for v in values
                )
                + "。"
            )
        else:
            text.append("当前未读到类型、单位与归属均可核验的性能观测；不等于原资料没有结果，可查询原记录。")
        test = row["ref"].get("test_ref")
        methods = [v for v in row["values"] if v["field_id"] == "method.standard" and v.get("evidence_refs")]
        for v in methods:
            tied = test is not None and v.get("subject_ref") == test
            text.append(
                ("本测试下登记的方法（仍须核对原文）：" if tied else "来源方法描述（适用测试未确认）：")
                + value_text(v)
                + "。"
            )
        conditions = [
            v
            for v in row["values"]
            if test is not None
            and v["field_id"] == "condition.parameter"
            and v.get("subject_ref") == test
            and v.get("evidence_refs")
        ]
        if conditions:
            text.append(
                "该测试下登记的条件：" + "；".join(value_text(v) for v in conditions) + "；仍须核对原文与适用范围。"
            )
        else:
            text.append("当前测试条件尚未确认；共享方法或其他主体的厚度等条件不套用于本测试。")
        if not methods:
            text.append("当前读取未提供可引用的方法描述，需回查原文；不等于论文未报告。")
    text.append("来源测量或报告不是独立科学验证；不同候选的测试条件尚未核对为可比，不计算差值、排名或新配方性能。")
    text.append("三、制备安排（来源参数与建议分开）")
    seen = set()
    for parameter in result["parameters"]:
        key = canonical(parameter)
        if key in seen:
            continue
        seen.add(key)
        text.append(
            "来源："
            + parameter["ref"]["source_id"]
            + "；过程："
            + parameter["process_ref"]
            + "；步骤："
            + (parameter.get("step_ref") or "未分步")
            + "；"
            + value_text(parameter["value"])
            + "。"
        )
    if not result["parameters"]:
        text.append("当前未读到带完整过程归属的制备参数；不表示原论文没有制备内容。")
    text.append(
        "以上来源参数不能直接作为新实验设置。建议核对配方基准、原料身份与用量，确认各过程/步骤适用于哪个候选、步骤顺序及新实验设置；温度、时长、混合比例缺项分别待确认，不跨步骤补齐。"
    )
    text.append("四、测试安排（定性建议）")
    text.append(
        "按原用户目标逐项确定测量指标；回查相应方法标准，核对试样制备、厚度、处理环境及其他测试条件。记录每个观测所属样品、批次、测试和原始数据；方法名称相同不代表条件相同。"
    )
    text.append("五、对照与复测（定性建议）")
    text.append(
        "选择同体系且条件可核对的基线对照；明确拟改变与保持的因素。由研究者确定独立制样、复测安排及记录方式，区分独立样品与重复读数；本草案不指定次数或虚构对照结果。"
    )
    text.append("六、判定标准（来自原用户目标）")
    text.append(render_target(target))
    text.append(
        "条件、单位和配方基准核实后，以这些原始目标逐项核对实际观测；缺失、冲突或归属不明时保留未知，不自动判为满足或不满足。不新增性能门槛，不以本草案宣称优化成功。"
    )
    text.append("七、待确认事项")
    text.append(
        "待补齐：候选配方基准与原料、过程步骤顺序及各步骤新实验设置、测试标准适用性和条件、基线对照、独立制样/复测安排与记录方式。关键参数未齐，本草案不是可执行SOP；核对并形成可审阅设置后再由人决定是否实验，当前未提交任何任务。"
    )
    return "\n".join(text)
