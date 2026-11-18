export const clone = (value) => structuredClone(value);
export function columnName(n) {
  let result = "";
  while (n > 0) {
    n--;
    result = String.fromCharCode(65 + (n % 26)) + result;
    n = Math.floor(n / 26);
  }
  return result;
}
export function columnNumber(value) {
  if (Number.isInteger(value)) return value;
  return String(value)
    .toUpperCase()
    .split("")
    .reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
}
export class DraftSession {
  constructor(draft, api) {
    this.draft = clone(draft);
    this.api = api;
    this.dirty = false;
    this.conflict = false;
    this.change = 0;
    this.pending = null;
  }
  edit(patch) {
    Object.assign(this.draft, clone(patch));
    this.dirty = true;
    this.change++;
  }
  async save() {
    if (this.conflict)
      throw Object.assign(new Error("草稿已在别处修改，请选择如何继续。"), {
        status: 409,
      });
    if (this.pending) return this.pending;
    this.pending = this.flush();
    try {
      return await this.pending;
    } finally {
      this.pending = null;
    }
  }
  async flush() {
    while (this.dirty) {
      const sequence = this.change;
      const body = {
        expected_revision: this.draft.revision,
        title: this.draft.title,
        recipe: clone(this.draft.recipe),
        reader_options: clone(this.draft.reader_options),
      };
      let saved;
      try {
        saved = await this.api(
          "/api/table-drafts/" + encodeURIComponent(this.draft.source_id),
          { method: "PUT", json: body },
        );
      } catch (e) {
        if (e.status === 409) this.conflict = true;
        throw e;
      }
      if (sequence === this.change) {
        this.draft = saved;
        this.dirty = false;
      } else {
        this.draft = {
          ...saved,
          title: this.draft.title,
          recipe: this.draft.recipe,
          reader_options: this.draft.reader_options,
        };
      }
    }
    return this.draft;
  }
}
export function uploadBatch(files) {
  return Object.freeze([...files]);
}
export function uploadPolicy(capabilities = {}, documentMode = false) {
  if (documentMode) return { maxBytes: 128 * 1024 * 1024, label: "128 MiB" };
  const declared = capabilities?.table_limits?.max_bytes;
  const maxBytes =
    Number.isSafeInteger(declared) && declared > 0 ? declared : 50_000_000;
  return { maxBytes, label: `${maxBytes / 1_000_000} MB` };
}
