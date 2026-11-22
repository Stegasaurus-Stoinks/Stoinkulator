// Placeholder: the compare view is a later task.
import { comingSoon } from "../api.js";

export function render(container, params) {
  comingSoon(container, "Compare runs", { runs: params.runIds.join(", ") });
}
