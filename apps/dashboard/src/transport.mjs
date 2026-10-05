// Transport sequence is local to each socket; gaps require a new full snapshot.
export function mergeBatch(previous, batch, lastSequence) {
  if (
    !Number.isSafeInteger(batch.sequence) ||
    batch.sequence < 0 ||
    !batch.data
  )
    throw new Error("Invalid batch");
  if (batch.kind === "snapshot")
    return { data: batch.data, sequence: batch.sequence };
  if (
    batch.kind !== "patch" ||
    previous === null ||
    batch.sequence !== lastSequence + 1
  )
    throw new Error("Snapshot required");
  function deep(base, patch) {
    if (Array.isArray(base) && patch && patch.$merge_by) {
      const key = patch.$merge_by,
        rows = new Map(base.map((row) => [row[key], row]));
      for (const row of patch.$rows) rows.set(row[key], row);
      return patch.$retain.map((id) => rows.get(id));
    }
    if (
      patch &&
      typeof patch === "object" &&
      !Array.isArray(patch) &&
      base &&
      typeof base === "object" &&
      !Array.isArray(base)
    ) {
      const out = { ...base };
      for (const [key, value] of Object.entries(patch))
        out[key] = deep(base[key], value);
      return out;
    }
    return patch;
  }
  return { data: deep(previous, batch.data), sequence: batch.sequence };
}
export function displayFresh(receivedAt, now = Date.now()) {
  return now - receivedAt <= 1500;
}
