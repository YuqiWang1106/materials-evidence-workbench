/** A late save may finish off-screen, but cannot replace a newer editor's scratch. */
export class DraftEditorOwnership {
  constructor() { this.current = new Map(); }
  acquire(sourceId) {
    const token = Symbol(sourceId);
    this.current.set(sourceId, token);
    return () => this.current.get(sourceId) === token;
  }
}
