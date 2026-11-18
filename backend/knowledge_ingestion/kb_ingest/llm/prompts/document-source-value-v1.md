
SOURCE VALUE CONTRACT (also used by the program and source review):
Keep the complete original cell as evidence. raw_value is the scientific value,
not necessarily the entire cell: a scalar contains only the original numeric
substring, and raw_unit contains the separately reported unit. Preserve exact
precision, sign, zero, intervals, uncertainty and notation. A bounded value or
value with an error term is not a scalar; do not drop its comparator or error.
For a reliable supplied table_cell, value_cell must be its exact existing ID.
Prefer raw_value=null to request deterministic source-cell reading. For numeric
facts the program projects the complete numeric expression and any inline unit;
a non-null raw_value must match that full numeric expression without rounding,
unit conversion or changed spelling. Text/category values preserve the whole
cell. Never invent a cell or its coordinates. An ambiguous cell stays unresolved.
An inline unit must not conflict with raw_unit. A unit from a header or methods
paragraph, and every proportion basis, needs that actual source evidence and
explicit applicability to this fact. Missing unit/basis is null, not a guessed
convention. raw_basis quotes the original basis wording, not a normalized alias.
For narrative facts use value_cell=null and cite the actual source paragraph.
The program checks the field definition, owner, value type and source boundaries;
valid JSON, matching numbers and shared table membership do not prove attribution.
