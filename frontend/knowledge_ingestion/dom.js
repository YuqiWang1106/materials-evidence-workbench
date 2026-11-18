// @ts-check
// Some native/accessibility button activations do not blur the active textbox.
// Keep editing quiet, then commit its latest value before running an action.
const pendingInputs = new Set();
export function bindInput(node, change) {
  let committed = String(node.value ?? '');
  const commit = () => {
    pendingInputs.delete(node);
    const value = String(node.value ?? '');
    if (value === committed) return;
    committed = value;
    change();
  };
  node.addEventListener('input', () => pendingInputs.add(node));
  node.addEventListener('change', commit);
}
export function commitPendingInputs() {
  for (const node of [...pendingInputs]) {
    pendingInputs.delete(node);
    if (node.isConnected !== false) node.dispatchEvent(new Event('change'));
  }
}
export function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = String(text);
  return n;
}
export function button(text, action, kind = "secondary") {
  const n = el("button", "button " + kind, text);
  n.type = "button";
  n.addEventListener("click", event => { commitPendingInputs(); action(event); });
  return n;
}
/**
 * @param {string | number} value
 * @param {(value: string) => void} change
 * @param {{type?: string, placeholder?: string, min?: number, max?: number}} [options]
 */
export function input(
  value,
  change,
  { type = "text", placeholder = "", min, max } = {},
) {
  const n = el("input");
  n.type = type;
  n.value = value ?? "";
  n.placeholder = placeholder;
  if (min !== undefined) n.min = min;
  if (max !== undefined) n.max = max;
  bindInput(n, () => change(n.value));
  return n;
}
export function select(options, value, change) {
  const n = el("select");
  for (const item of options) {
    const [key, label] = Array.isArray(item) ? item : [item, item];
    const o = el("option", null, label);
    o.value = key;
    n.append(o);
  }
  n.value = value ?? "";
  n.addEventListener("change", () => { commitPendingInputs(); change(n.value); });
  return n;
}
export function field(label, control, hint) {
  const n = el("label", "field");
  n.append(el("span", "field-label", label), control);
  if (hint) n.append(el("small", "help", hint));
  return n;
}
export function row(...children) {
  const n = el("div", "form-row");
  n.append(...children);
  return n;
}
export function textValue(value) {
  return value === null || value === undefined
    ? "空白"
    : value === ""
      ? "空字符串"
      : String(value);
}
