// The little the shell and the pages need to share at the same moment: the fleet as it
// was last read (so the command palette can find an Android without waiting) and the
// selection made on the Androids page (so "start the selection" works from anywhere).
// Nothing here is a second source of truth: pages still read the API themselves.

export const fleet = {
  instances: [],
  selection: new Set(),
};

export function rememberInstances(instances) {
  fleet.instances = instances || [];
  const known = new Set(fleet.instances.map((i) => i.id));
  for (const id of [...fleet.selection]) if (!known.has(id)) fleet.selection.delete(id);
}

export function rememberSelection(ids) {
  fleet.selection = new Set(ids);
}

// Tell every open page that something on the farm changed and it should read again.
export const changed = () => window.dispatchEvent(new CustomEvent('sf:changed'));
