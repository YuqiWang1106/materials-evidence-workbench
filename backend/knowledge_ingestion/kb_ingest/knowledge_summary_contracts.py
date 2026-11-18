"""Fixed, versioned conversation summary content; ordinary Wiki pages stay flexible."""

from typing import Literal

from pydantic import Field, model_validator

from .knowledge_claim_contracts import KnowledgeClaim, KnowledgeSection, WikiContent

TEMPLATE_VERSION = "experimental-analysis-v1"
SECTIONS = (
    ("research_goal", "研究目标"),
    ("data_description", "数据说明"),
    ("conditions_variables", "已知条件与变量"),
    ("completed_analysis", "已完成分析"),
    ("results_conclusions", "结果与结论"),
    ("gaps_scope", "缺口与适用范围"),
    ("next_steps", "后续建议"),
)


class SummaryContent(WikiContent):
    template_version: Literal["experimental-analysis-v1"]

    @model_validator(mode="after")
    def fixed_template(self):
        if [(s.section_id, s.heading) for s in self.sections] != list(SECTIONS):
            raise ValueError("INVALID_SUMMARY_TEMPLATE")
        if any(not s.claims or any(not c.text.strip() for c in s.claims) for s in self.sections):
            raise ValueError("EMPTY_SUMMARY_SECTION")
        return self


class TableSummaryClaim(KnowledgeClaim):
    # Paper provenance is a server-owned record, not a free-form model output.
    # Inheriting JsonValue here would emit an untyped definition in strict JSON.
    research_provenance: None = None


class TableSummarySection(KnowledgeSection):
    claims: list[TableSummaryClaim] = Field(default_factory=list, max_length=200)


class TableSummaryOutput(SummaryContent):
    sections: list[TableSummarySection] = Field(default_factory=list, max_length=200)


def output_schema():
    """Responses strict schemas require every declared object property to be required."""
    schema = TableSummaryOutput.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema
