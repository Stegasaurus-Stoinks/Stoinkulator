// Placeholder: the compare view is a later task.
import { comingSoon } from "../dom.js";

export function render(container, params) {
  comingSoon(container, "Compare runs", { runs: params.runIds.join(", ") });
}
