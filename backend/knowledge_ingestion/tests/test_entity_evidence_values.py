"""Edited quantities must retain the value, unit and qualifier in their own quotation."""

import pytest
from kb_ingest.extraction.entity_evidence_values import normalized_evidence_text, value_evidence_issues


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("LOI 30.0 %", "LOI 30%"),
        ("温度为 −20 ℃", "温度为 -20 °C"),
        ("热流 35 kW/m2", "热流 35 kW/m²"),
        ("density 1.20 g/cm^3", "density 1.2 g/cm³"),
        ("stress 1e3 Pa", "stress 1 × 10³ Pa"),
        ("添加量 10 wt.%", "添加量 10 wt %"),
        ("温度 20 至 30 °C", "温度 20–30 ℃"),
        ("LOI ≥ 30 %", "LOI >=30%"),
        ("LOI 30 ± 0.5 %", "LOI 30±0.50%"),
        ("按 ASTM D256 测得 12 kJ/m²", "按 ASTM D256 测得 12 kJ/m²"),
    ],
)
def test_equivalent_quantity_notation_is_supported(edited, quote):
    assert value_evidence_issues(edited, quote) == []


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("LOI 99%", "LOI 30%"),
        ("LOI 30 wt%", "LOI 30%"),
        ("添加量 30 vol%", "添加量 30 wt%"),
        ("LOI 30", "LOI 30%"),
        ("LOI 30%", "LOI 30"),
        ("温度 30 s，时间 10 °C", "温度 30 °C，时间 10 s"),
        ("热流 35 kW/m³", "热流 35 kW/m²"),
        ("压力 1 mPa", "压力 1 MPa"),
        ("温度 20 °C", "温度 -20 °C"),
        ("应力 1e-3 Pa", "应力 1e3 Pa"),
        ("LOI >30%", "LOI ≥30%"),
        ("LOI 30%", "LOI ≥30%"),
        ("温度 20 °C", "温度 20–30 °C"),
        ("温度 20–40 °C", "温度 20–30 °C"),
        ("LOI 30 ± 1 %", "LOI 30±0.5%"),
        ("LOI 30 %", "LOI 30±0.5%"),
        ("LOI20%，失重率30%", "LOI30%，失重率20%"),
    ],
)
def test_changed_quantity_cannot_borrow_a_matching_number(edited, quote):
    issues = value_evidence_issues(edited, quote)
    assert issues and all(row["code"] and row["message"] for row in issues)


def test_numeric_entity_name_cannot_hide_a_changed_measurement():
    assert value_evidence_issues("LOI 99%", "LOI 30%", names=["LOI 99%", "99%"])
    assert value_evidence_issues("Ti3C2Tx 的 LOI 30%", "Ti3C2Tx 的 LOI 30%", names=["Ti3C2Tx"]) == []


def test_unknown_unit_is_compared_conservatively_without_conversion():
    assert value_evidence_issues("性能 3 widgets", "性能 3 widgets") == []
    assert value_evidence_issues("性能 3 widgets", "性能 3 gadgets")
    assert value_evidence_issues("长度 1 m", "长度 100 cm")


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("LOI：３０％", "LOI 30%"),
        ("强度 30.0 MPa", "强度 30 MPa"),
        ("温度20至30℃", "温度20–30 °C"),
        ("应力 1e3 Pa", "应力 1 × 10³ Pa"),
        ("热流35kW/m2", "热流35 kW/m²"),
    ],
)
def test_wording_normalization_keeps_only_safe_quantity_and_layout_equivalences(edited, quote):
    assert normalized_evidence_text(edited) == normalized_evidence_text(quote)


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("LOI20%，失重30%", "LOI30%，失重20%"),
        ("单位MPa", "单位mPa"),
        ("单位W", "单位mW"),
        ("配比1:2", "配比12"),
        ("配比1:2", "配比1 2"),
    ],
)
def test_wording_normalization_preserves_property_pairings_unit_scale_and_ratios(edited, quote):
    assert normalized_evidence_text(edited) != normalized_evidence_text(quote)


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("配比1", "配比1:2"),
        ("配比1:2", "配比1:2:3"),
        ("温度20℃", "温度20℃以下"),
        ("LOI30%", "LOI至少30%"),
        ("LOI30%", "LOI at least 30%"),
    ],
)
def test_edited_value_cannot_drop_ratio_components_or_comparison_conditions(edited, quote):
    assert value_evidence_issues(edited, quote)


@pytest.mark.parametrize("separator", ["×", "x", "*"])
def test_dimensions_remain_one_quantity_and_cannot_drop_a_side(separator):
    quote = f"尺寸 5 mm {separator} 10 mm {separator} 20 mm"
    assert value_evidence_issues("尺寸 5 mm", quote)
    assert value_evidence_issues(f"尺寸 5 mm {separator} 10 mm", quote)
    edited = "尺寸5.0mm×10.0mm×20.0mm"
    assert value_evidence_issues(edited, quote) == []
    assert normalized_evidence_text(edited) == normalized_evidence_text(quote)


@pytest.mark.parametrize("separator", ["×", "x", "*"])
def test_dimensions_may_share_one_trailing_unit(separator):
    quote = f"尺寸 5 {separator} 10 mm"
    assert value_evidence_issues("尺寸5mm", quote)
    assert value_evidence_issues("尺寸5 mm×10 mm", quote) == []


@pytest.mark.parametrize("identifier", ["S1", "D256", "ISO4589"])
def test_identifier_does_not_consume_the_following_property_label(identifier):
    quote = identifier + " LOI 不低于30%。"
    edited = "LOI >=30.0%"
    assert value_evidence_issues(edited, quote) == []
    normalized = normalized_evidence_text(quote)
    assert normalized.startswith(identifier + "LOI")
    assert normalized_evidence_text(edited) in normalized


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("值10 ms", "值10 m s"),
        ("值10 mm", "值10 m m"),
        ("值10 mPa", "值10 m Pa"),
    ],
)
def test_unit_factor_spaces_cannot_be_deleted_into_si_prefixes(edited, quote):
    assert value_evidence_issues(edited, quote)
    assert normalized_evidence_text(edited) != normalized_evidence_text(quote)


@pytest.mark.parametrize(
    ("edited", "quote"),
    [
        ("值10 kg·m^-3", "值10 kg m ^ -3"),
        ("值10 W/(m·K)", "值10 W / (m K)"),
        ("值10 kW/m^2", "值10 kW / m ^ 2"),
    ],
)
def test_unit_operator_whitespace_is_layout_but_factor_boundaries_remain(edited, quote):
    assert value_evidence_issues(edited, quote) == []
    assert normalized_evidence_text(edited) == normalized_evidence_text(quote)


@pytest.mark.parametrize(
    "text",
    [
        "ISO30",
        "ASTM30",
        "UL94",
        "DIN30",
        "GB30",
        "iso30",
        "astm30",
        "ul94",
        "din30",
        "gb30",
        "S1",
        "s1",
        "AB30foo",
        "A30foo",
    ],
)
def test_standard_sample_and_literal_adjacent_identifiers_are_not_quantities(text):
    assert normalized_evidence_text(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("AB30", "AB⟦3e1|||⟧"),
        ("Ab30", "Ab⟦3e1|||⟧"),
        ("AB30 foo", "AB⟦3e1|literal:foo||⟧"),
        ("XY30%", "XY⟦3e1|%||⟧"),
        ("LOI30%", "LOI⟦3e1|%||⟧"),
    ],
)
def test_longer_prefix_or_a_real_unit_keeps_the_quantity(text, expected):
    assert normalized_evidence_text(text) == expected
